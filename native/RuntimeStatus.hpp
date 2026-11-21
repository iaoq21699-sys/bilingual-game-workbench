#pragma once

// Local startup telemetry only. Render/database threads update a small snapshot;
// one background writer owns all disk I/O, including the two-second heartbeat.
#include <windows.h>
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <filesystem>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>

namespace bilingual {
class RuntimeStatus {
    struct Runtime {
        bool ready=false,overlay=false,input=false,drawn=false;
        std::string error_code,error_detail;
    };
    struct Snapshot {
        bool addon=false,corpus=false;
        std::int64_t count=-1;
        Runtime runtime;
        std::string game_id,error_code,error_detail;
    };
    DWORD pid_=GetCurrentProcessId();
    std::uint64_t start_=0;
    std::filesystem::path target_;
    std::mutex mutex_;
    std::condition_variable wake_;
    Snapshot state_;
    std::unordered_map<const void*,Runtime> runtimes_;
    const void* selected_=nullptr;
    std::uint64_t revision_=1;
    bool stopping_=false;
    std::thread writer_;

    static std::uint64_t number(FILETIME value) noexcept {
        return (static_cast<std::uint64_t>(value.dwHighDateTime)<<32)|value.dwLowDateTime;
    }
    static std::uint64_t now_ms() noexcept {
        FILETIME now{};GetSystemTimeAsFileTime(&now);
        return (number(now)-116444736000000000ULL)/10000;
    }
    static std::string bounded(const std::string& value,std::size_t maximum=4096) {
        return value.substr(0,maximum);
    }
    // Escape controls and replace malformed/truncated UTF-8, so diagnostics from
    // platform exceptions cannot turn the status into invalid JSON.
    static std::string quote(const std::string& value) {
        static const char hex[]="0123456789abcdef";
        std::string out="\"";
        for(std::size_t i=0;i<value.size();) {
            const unsigned char c=static_cast<unsigned char>(value[i]);
            if(c=='"'||c=='\\') {out+='\\';out+=static_cast<char>(c);++i;}
            else if(c<32) {out+="\\u00";out+=hex[c>>4];out+=hex[c&15];++i;}
            else if(c<128) {out+=static_cast<char>(c);++i;}
            else {
                unsigned length=c>=0xc2&&c<=0xdf?2:c>=0xe0&&c<=0xef?3:c>=0xf0&&c<=0xf4?4:0;
                bool valid=length && i+length<=value.size();
                for(unsigned j=1;valid&&j<length;++j) valid=(static_cast<unsigned char>(value[i+j])&0xc0)==0x80;
                if(valid&&length>=3) {
                    const unsigned char second=static_cast<unsigned char>(value[i+1]);
                    valid=!(c==0xe0&&second<0xa0)&&!(c==0xed&&second>=0xa0)&&
                          !(c==0xf0&&second<0x90)&&!(c==0xf4&&second>=0x90);
                }
                if(valid) {out.append(value,i,length);i+=length;}
                else {out+="\xef\xbf\xbd";++i;}
            }
        }
        out+='"';return out;
    }
    static const char* boolean(bool value) noexcept{return value?"true":"false";}
    std::string encode(const Snapshot& s) const {
        const std::string code=s.error_code.empty()?s.runtime.error_code:s.error_code;
        const std::string detail=s.error_code.empty()?s.runtime.error_detail:s.error_detail;
        return "{\n\"schema_version\":1,\n\"addon_version\":\"0.7.0\",\n\"pid\":"+std::to_string(pid_)+
            ",\n\"process_start_filetime\":\""+std::to_string(start_)+"\",\n\"game_id\":"+quote(s.game_id)+
            ",\n\"updated_at_ms\":"+std::to_string(now_ms())+
            ",\n\"addon_loaded\":"+boolean(s.addon)+",\n\"runtime_ready\":"+boolean(s.runtime.ready)+
            ",\n\"overlay_ready\":"+boolean(s.runtime.overlay)+",\n\"input_ready\":"+boolean(s.runtime.input)+
            ",\n\"corpus_ready\":"+boolean(s.corpus)+",\n\"corpus_count\":"+std::to_string(s.count)+
            ",\n\"panel_drawn\":"+boolean(s.runtime.drawn)+",\n\"error_code\":"+quote(code)+
            ",\n\"error_detail\":"+quote(detail)+"\n}\n";
    }
    void changed() {++revision_;wake_.notify_one();}
    void write(const Snapshot& state) noexcept {
        try {
            const std::string bytes=encode(state);
            if(bytes.size()>64*1024) return;
            auto temporary=target_;
            temporary+=L"."+std::to_wstring(start_)+L".tmp";
            HANDLE file=CreateFileW(temporary.c_str(),GENERIC_WRITE,0,nullptr,CREATE_ALWAYS,FILE_ATTRIBUTE_NORMAL,nullptr);
            if(file==INVALID_HANDLE_VALUE) {
                OutputDebugStringW(L"BilingualPanel: cannot create local health status file.\n");return;
            }
            DWORD written=0;
            const DWORD count=static_cast<DWORD>(bytes.size());
            const bool okay=WriteFile(file,bytes.data(),count,&written,nullptr)&&written==count&&FlushFileBuffers(file);
            CloseHandle(file);
            if(!okay||!MoveFileExW(temporary.c_str(),target_.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH)) {
                DeleteFileW(temporary.c_str());
                OutputDebugStringW(L"BilingualPanel: cannot atomically publish local health status.\n");
            }
        } catch(...) {OutputDebugStringW(L"BilingualPanel: health status serialization failed.\n");}
    }
    void run() noexcept {
        try {
            std::unique_lock lock(mutex_);
            for(;;) {
                const auto version=revision_;
                const bool final=stopping_;
                Snapshot snapshot=state_;
                if(const auto found=runtimes_.find(selected_);found!=runtimes_.end()) snapshot.runtime=found->second;
                lock.unlock();write(snapshot);lock.lock();
                if(final) break;
                wake_.wait_for(lock,std::chrono::seconds(2),[&]{return stopping_||revision_!=version;});
            }
        } catch(...) {OutputDebugStringW(L"BilingualPanel: health writer stopped unexpectedly.\n");}
    }
public:
    RuntimeStatus() {
        FILETIME created{},exited{},kernel{},user{};
        if(!GetProcessTimes(GetCurrentProcess(),&created,&exited,&kernel,&user))
            throw std::runtime_error("Cannot read process creation time for health status");
        start_=number(created);
        std::wstring executable(32768,L'\0');
        const DWORD size=GetModuleFileNameW(nullptr,executable.data(),static_cast<DWORD>(executable.size()));
        if(!size||size>=executable.size()) throw std::runtime_error("Cannot locate game executable for health status");
        executable.resize(size);
        target_=std::filesystem::path(executable).parent_path()/(L"BilingualPanel.status."+std::to_wstring(pid_)+L".json");
        writer_=std::thread([this]{run();});
    }
    ~RuntimeStatus() {stop();}
    RuntimeStatus(const RuntimeStatus&)=delete;
    RuntimeStatus& operator=(const RuntimeStatus&)=delete;
    void stop() noexcept {
        {std::lock_guard lock(mutex_);stopping_=true;wake_.notify_one();}
        if(writer_.joinable()) writer_.join();
    }
    void game(const std::string& id) {
        std::lock_guard lock(mutex_);state_.game_id=bounded(id,256);changed();
    }
    void loaded() {
        std::lock_guard lock(mutex_);state_.addon=true;changed();
    }
    void unloaded() {
        std::lock_guard lock(mutex_);state_.addon=false;state_.corpus=false;
        selected_=nullptr;runtimes_.clear();
        if(state_.error_code.empty()) {state_.error_code="addon_unloaded";state_.error_detail="The add-on was unloaded from this process.";}
        changed();
    }
    void error(const std::string& code,const std::string& detail,bool database=false) {
        std::lock_guard lock(mutex_);
        state_.error_code=bounded(code,128);state_.error_detail=bounded(detail);
        if(database) state_.corpus=false;
        changed();
    }
    void corpus_count(std::int64_t count) {
        std::lock_guard lock(mutex_);state_.count=std::max<std::int64_t>(0,count);state_.corpus=count>0;
        if(!count) {state_.error_code="empty_corpus";state_.error_detail="No text entries are available for the configured game_id.";}
        else if(state_.error_code=="db_error"||state_.error_code=="empty_corpus") {state_.error_code.clear();state_.error_detail.clear();}
        changed();
    }
    void query_succeeded() {
        std::lock_guard lock(mutex_);
        if(state_.error_code=="query_error"||(state_.error_code=="db_error"&&state_.count>0)) {
            state_.error_code.clear();state_.error_detail.clear();
            if(state_.count>0) state_.corpus=true;
            changed();
        }
    }
    void configuration_saved() {
        std::lock_guard lock(mutex_);
        if(state_.error_code=="config_write_error") {state_.error_code.clear();state_.error_detail.clear();changed();}
    }
    void runtime_initialized(const void* runtime) {
        std::lock_guard lock(mutex_);
        if(runtimes_.size()>=64&&!runtimes_.contains(runtime)) return;
        runtimes_[runtime]=Runtime{true};
        if(!selected_) selected_=runtime;
        changed();
    }
    void runtime_destroyed(const void* runtime) {
        std::lock_guard lock(mutex_);runtimes_.erase(runtime);
        if(selected_==runtime) selected_=nullptr;
        changed();
    }
    void overlay(const void* runtime,bool input_ready,bool drawn) {
        std::lock_guard lock(mutex_);
        if(runtimes_.size()>=64&&!runtimes_.contains(runtime)) return;
        auto& current=runtimes_[runtime];
        // A real overlay callback also proves this exact runtime initialized,
        // even when registration happened after its init event.
        bool change=!current.ready||!current.overlay||current.input!=input_ready||(drawn&&!current.drawn);
        current.ready=true;current.overlay=true;current.input=input_ready;current.drawn|=drawn;
        if(!input_ready) {
            if(current.error_code.empty()||current.error_code=="input_not_ready") {
                change|=current.error_code.empty();
                current.error_code="input_not_ready";
                current.error_detail="No valid game input window is connected to this rendering runtime.";
            }
        } else if(current.error_code=="input_not_ready") {current.error_code.clear();current.error_detail.clear();change=true;}
        // Select one runtime, never OR readiness flags across different windows.
        const auto selected=runtimes_.find(selected_);
        if(!selected_||selected==runtimes_.end()||!selected->second.overlay) {selected_=runtime;change=true;}
        if(change) changed();
    }
    void runtime_error(const void* runtime,const std::string& code,const std::string& detail) {
        std::lock_guard lock(mutex_);
        if(runtimes_.size()>=64&&!runtimes_.contains(runtime)) return;
        auto& current=runtimes_[runtime];
        current.error_code=bounded(code,128);current.error_detail=bounded(detail);
        current.drawn=false;
        if(!selected_) selected_=runtime;
        changed();
    }
};
}
