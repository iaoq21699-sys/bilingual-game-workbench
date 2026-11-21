// ReShade add-on: renders in the game's swap chain, with no desktop window.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#define IMGUI_DISABLE_INCLUDE_IMCONFIG_H
#define ImTextureID ImU64
#include <imgui.h>
#include <reshade.hpp>
#include "shared/Corpus.hpp"
#include "shared/NoteWriter.hpp"
#include "WindowInput.hpp"
#include "RuntimeStatus.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cctype>
#include <cmath>
#include <deque>
#include <filesystem>
#include <fstream>
#include <memory>
#include <mutex>
#include <iomanip>
#include <sstream>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace {
using bilingual::data::Corpus;
using bilingual::data::Entry;
using bilingual::data::LookupHit;
using bilingual::data::SearchPage;
using bilingual::data::SearchRequest;
using bilingual::notes::Note;
using bilingual::notes::Receipt;
using Clock = std::chrono::steady_clock;
constexpr int kPageSize = 30;
constexpr ImVec4 rgb(unsigned value) {
    return ImVec4(((value>>16)&255)/255.0f,((value>>8)&255)/255.0f,(value&255)/255.0f,1.0f);
}
struct Theme {
    ImVec4 background=rgb(0x161412),panel=rgb(0x211F1B),text=rgb(0xF2EBDB);
    ImVec4 muted=rgb(0xA39C8C),accent=rgb(0xD9BD87),border=rgb(0x574D3B);
    std::string name="暖炭金",source="preset:warm";
};
const std::array<const char*,6> kThemeKeys{"Background","Panel","Text","Muted","Accent","Border"};
const std::array<const char*,6> kThemeLabels{"画面背景","控件底色","正文","辅助文字","强调色","边线"};
const std::array<const char*,3> kPresetNames{"暖炭金","冷蓝灰","中性灰"};
const std::array<const char*,3> kPresetIds{"warm","cool","neutral"};

Theme preset_theme(int index) {
    Theme result;
    if(index==1) {
        result.background=rgb(0x121922);result.panel=rgb(0x1C2936);result.text=rgb(0xEAF3FA);
        result.muted=rgb(0xA1B5C7);result.accent=rgb(0x85BFE2);result.border=rgb(0x456078);
    } else if(index==2) {
        result.background=rgb(0x171717);result.panel=rgb(0x242424);result.text=rgb(0xF1F1F1);
        result.muted=rgb(0xABABAB);result.accent=rgb(0xC9C9C9);result.border=rgb(0x555555);
    }
    result.name=kPresetNames[std::clamp(index,0,2)];
    result.source=std::string("preset:")+kPresetIds[std::clamp(index,0,2)];
    return result;
}
ImVec4* theme_color(Theme& theme,std::string_view key) {
    if(key=="Background") return &theme.background;
    if(key=="Panel") return &theme.panel;
    if(key=="Text") return &theme.text;
    if(key=="Muted") return &theme.muted;
    if(key=="Accent") return &theme.accent;
    if(key=="Border") return &theme.border;
    return nullptr;
}
bool parse_color(const std::string& value,ImVec4& color) {
    if(value.size()!=7 || value.front()!='#') return false;
    unsigned number=0;
    for(std::size_t i=1;i<value.size();++i) {
        const unsigned char ch=static_cast<unsigned char>(value[i]);
        unsigned digit;
        if(ch>='0'&&ch<='9') digit=ch-'0';
        else if(ch>='a'&&ch<='f') digit=ch-'a'+10;
        else if(ch>='A'&&ch<='F') digit=ch-'A'+10;
        else return false;
        number=(number<<4)|digit;
    }
    color=rgb(number);return true;
}
std::string hex_color(const ImVec4& color) {
    std::ostringstream result;
    result << '#' << std::uppercase << std::hex << std::setfill('0');
    for(float channel:{color.x,color.y,color.z}) result << std::setw(2) << static_cast<int>(std::lround(std::clamp(channel,0.0f,1.0f)*255));
    return result.str();
}
double luminance(const ImVec4& color) {
    const auto linear=[](float channel) {const double value=channel;return value<=0.04045?value/12.92:std::pow((value+0.055)/1.055,2.4);};
    return 0.2126*linear(color.x)+0.7152*linear(color.y)+0.0722*linear(color.z);
}
double contrast(const ImVec4& a,const ImVec4& b) {
    const auto x=luminance(a),y=luminance(b);
    return (std::max(x,y)+0.05)/(std::min(x,y)+0.05);
}
bool protect_contrast(Theme& theme) {
    bool adjusted=false;
    for(const auto* key:kThemeKeys) {
        auto& color=*theme_color(theme,key);
        const auto channel=[](float value) {return std::round(std::clamp(value,0.0f,1.0f)*255.0f)/255.0f;};
        color.x=channel(color.x);color.y=channel(color.y);color.z=channel(color.z);color.w=1.0f;
    }
    const ImVec4 black=rgb(0),white=rgb(0xFFFFFF);
    auto minimum=[&theme](const ImVec4& color) {return std::min(contrast(color,theme.background),contrast(color,theme.panel));};
    if(std::max(minimum(black),minimum(white))<4.5) {theme.panel=theme.background;adjusted=true;}
    const auto anchor=minimum(white)>=minimum(black)?white:black;
    for(auto* color:{&theme.text,&theme.muted,&theme.accent}) {
        if(minimum(*color)<4.5) {*color=anchor;adjusted=true;}
    }
    return adjusted;
}
ImVec4 blend(const ImVec4& a,const ImVec4& b,float amount,float alpha=1.0f) {
    return ImVec4(a.x+(b.x-a.x)*amount,a.y+(b.y-a.y)*amount,a.z+(b.z-a.z)*amount,alpha);
}

std::string trim(std::string value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    return value.substr(first, value.find_last_not_of(" \t\r\n") - first + 1);
}

std::string utf8(const std::wstring& value) {
    if (value.empty()) return {};
    const int size = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (!size) throw std::runtime_error("Windows path cannot be converted to UTF-8");
    std::string result(size, '\0');
    WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value.data(),
        static_cast<int>(value.size()), result.data(), size, nullptr, nullptr);
    return result;
}

std::wstring wide_path(const std::string& value) {
    if (value.empty()) return {};
    const int size=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,value.data(),static_cast<int>(value.size()),nullptr,0);
    if(!size) throw std::runtime_error("Panel database path is not valid UTF-8");
    std::wstring result(size,L'\0');
    MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,value.data(),static_cast<int>(value.size()),result.data(),size);
    return result;
}

std::filesystem::path module_directory(HMODULE module) {
    std::wstring path(32768, L'\0');
    const DWORD size = GetModuleFileNameW(module, path.data(), static_cast<DWORD>(path.size()));
    if (!size || size >= path.size()) throw std::runtime_error("Cannot locate add-on directory");
    path.resize(size);
    return std::filesystem::path(path).parent_path();
}

struct Config {
    std::filesystem::path directory;
    std::string database, game_id, title = "游戏双语阅读";
    float opacity = 0.78f;
    bool pin_right = true;
    Theme theme;
};

std::string read_ini(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("BilingualPanel.ini is missing");
    input.seekg(0, std::ios::end);
    const auto length = input.tellg();
    if (length < 0 || length > 1024*1024) throw std::runtime_error("Panel configuration size is invalid");
    input.seekg(0);
    std::string content(static_cast<std::size_t>(length), '\0');
    if (!content.empty() && !input.read(content.data(),static_cast<std::streamsize>(content.size())))
        throw std::runtime_error("Panel configuration cannot be read");
    // Windows INI writers use UTF-16LE BOM. Portable hand-written profiles may
    // instead be UTF-8, with or without BOM; preserve Chinese paths in both.
    if (content.size()>=2 && ((static_cast<unsigned char>(content[0])==0xff && static_cast<unsigned char>(content[1])==0xfe)
        || (static_cast<unsigned char>(content[0])==0xfe && static_cast<unsigned char>(content[1])==0xff))) {
        const bool little=static_cast<unsigned char>(content[0])==0xff;
        if (content.size()%2) throw std::runtime_error("Panel UTF-16 configuration is truncated");
        std::wstring decoded;
        decoded.reserve((content.size()-2)/2);
        for(std::size_t i=2;i<content.size();i+=2) {
            const auto a=static_cast<unsigned char>(content[i]),b=static_cast<unsigned char>(content[i+1]);
            decoded.push_back(static_cast<wchar_t>(little?(a|(b<<8)):((a<<8)|b)));
        }
        content=utf8(decoded);
    }
    if(content.compare(0,3,"\xEF\xBB\xBF")==0) content.erase(0,3);
    return content;
}

Config read_config(HMODULE module) {
    Config config;
    config.directory = module_directory(module);
    const auto content=read_ini(config.directory/L"BilingualPanel.ini");
    std::istringstream lines(content);
    std::string line, section;
    bool first = true;
    while (std::getline(lines, line)) {
        if (first && line.compare(0, 3, "\xEF\xBB\xBF") == 0) line.erase(0, 3);
        first = false;
        line = trim(line);
        if (line.empty() || line.front() == ';' || line.front() == '#') continue;
        if (line.front() == '[' && line.back() == ']') { section = trim(line.substr(1, line.size()-2)); continue; }
        if (section != "Panel" && section != "Theme") continue;
        const auto equals = line.find('=');
        if (equals == std::string::npos) continue;
        const auto key = trim(line.substr(0, equals));
        auto value = trim(line.substr(equals+1));
        if (value.size()>1 && value.front()=='"' && value.back()=='"') value = value.substr(1, value.size()-2);
        if(section=="Theme") {
            if(auto* color=theme_color(config.theme,key)) parse_color(value,*color);
            else if(key=="Name" && !value.empty()) config.theme.name=value;
            else if(key=="Source" && !value.empty()) config.theme.source=value;
            continue;
        }
        if (key == "Database") config.database = value;
        else if (key == "GameId") config.game_id = value;
        else if (key == "Title" && !value.empty()) config.title = value;
        else if (key == "PinSide") config.pin_right = value != "left";
        else if (key == "Opacity") {
            try { const float opacity=std::stof(value);if(std::isfinite(opacity)) config.opacity = std::clamp(opacity, 0.35f, 0.95f); } catch (...) {}
        }
    }
    protect_contrast(config.theme);
    if (config.database.empty() || config.game_id.empty())
        throw std::runtime_error("Panel Database or GameId is missing");
    auto database = std::filesystem::path(wide_path(config.database));
    if (database.is_relative()) database = config.directory / database;
    config.database = utf8(database.lexically_normal().wstring());
    return config;
}

// Save only the requested values, preserving the latest game/database fields,
// unknown sections and comments. Replace the INI atomically on the same volume.
void save_panel_settings(const Config& config,Theme theme,float opacity,std::optional<bool> side) {
    std::vector<std::string> lines;
    std::istringstream input(read_ini(config.directory/L"BilingualPanel.ini"));
    std::string line;
    while(std::getline(input,line)) {if(!line.empty()&&line.back()=='\r') line.pop_back();lines.push_back(line);}
    const auto upsert=[&lines](const std::string& section,const std::string& key,const std::string& value) {
        constexpr auto missing=static_cast<std::size_t>(-1);
        std::size_t begin=missing,end=lines.size();
        for(std::size_t i=0;i<lines.size();++i) {
            const auto text=trim(lines[i]);
            if(text.size()>1 && text.front()=='[' && text.back()==']') {
                if(begin!=missing) {end=i;break;}
                if(trim(text.substr(1,text.size()-2))==section) begin=i+1;
            }
        }
        if(begin==missing) {
            if(!lines.empty()&&!lines.back().empty()) lines.emplace_back();
            lines.push_back('['+section+']');lines.push_back(key+" = "+value);return;
        }
        bool updated=false;
        for(std::size_t i=begin;i<end;++i) {
            const auto text=trim(lines[i]);const auto equal=text.find('=');
            if(equal!=std::string::npos && trim(text.substr(0,equal))==key) {lines[i]=key+" = "+value;updated=true;}
        }
        if(!updated) lines.insert(lines.begin()+static_cast<std::ptrdiff_t>(end),key+" = "+value);
    };
    if (side.has_value()) {
        upsert("Panel","PinSide",*side ? "right" : "left");
    } else {
        protect_contrast(theme);
        std::ostringstream alpha;
        alpha << std::fixed << std::setprecision(3) << std::clamp(opacity,0.35f,0.95f);
        upsert("Panel","Opacity",alpha.str());
        for(const auto* key:kThemeKeys) upsert("Theme",key,hex_color(*theme_color(theme,key)));
        upsert("Theme","Name",theme.name);upsert("Theme","Source",theme.source);
    }
    std::string content;
    for(const auto& item:lines) {content+=item;content+="\r\n";}
    std::wstring encoded(1,L'\xFEFF');encoded+=wide_path(content);
    const auto target=config.directory/L"BilingualPanel.ini";
    const auto temporary=config.directory/(L"BilingualPanel.ini."+std::to_wstring(GetCurrentProcessId())+L"."+std::to_wstring(GetTickCount64())+L".tmp");
    HANDLE file=CreateFileW(temporary.c_str(),GENERIC_WRITE,0,nullptr,CREATE_NEW,FILE_ATTRIBUTE_NORMAL,nullptr);
    if(file==INVALID_HANDLE_VALUE) throw std::runtime_error("Cannot create appearance configuration");
    const DWORD count=static_cast<DWORD>(encoded.size()*sizeof(wchar_t));
    DWORD written=0;
    const bool okay=WriteFile(file,encoded.data(),count,&written,nullptr) && written==count && FlushFileBuffers(file);
    CloseHandle(file);
    if(!okay || !MoveFileExW(temporary.c_str(),target.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH)) {
        DeleteFileW(temporary.c_str());throw std::runtime_error("Cannot save appearance configuration");
    }
}

void log_event(const Config& config, const char* event) noexcept {
    try {
        std::ofstream log(config.directory / L"BilingualPanel.log", std::ios::app);
        SYSTEMTIME now{};
        GetLocalTime(&now);
        log << now.wYear << '-' << now.wMonth << '-' << now.wDay << ' '
            << now.wHour << ':' << now.wMinute << ':' << now.wSecond << ' ' << event << '\n';
    } catch (...) {}
}

// Display only: preserve placeholders, keys and raw SQLite content.
std::string display_text(std::string value) {
    for (const std::string tag : {"[blue]", "[/blue]", "[gold]", "[/gold]"}) {
        std::size_t at = 0;
        while ((at = value.find(tag, at)) != std::string::npos) value.erase(at, tag.size());
    }
    // Unreal rich text uses <Style>...</> tags. Keep comparison signs and unknown data.
    for (std::size_t at = 0; (at = value.find('<', at)) != std::string::npos;) {
        const auto end = value.find('>', at+1);
        if (end == std::string::npos || end-at > 90) { ++at; continue; }
        const auto body = value.substr(at+1, end-at-1);
        if (!body.empty() && (std::isalpha(static_cast<unsigned char>(body[0])) || body[0]=='/') && body.find('\n')==std::string::npos)
            value.erase(at, end-at+1);
        else ++at;
    }
    return value;
}

enum class JobKind { Categories, Search, Entry, Lookup, SaveAppearance, SaveSide };
struct Job {
    JobKind kind = JobKind::Search;
    std::uint64_t generation = 0;
    std::string value, category;
    std::int64_t offset = 0;
    Theme theme;
    float opacity=0.78f;
    std::uint64_t appearance_revision=0;
    bool pin_right=true;
};
struct Reply {
    Job job;
    SearchPage page;
    std::optional<Entry> entry;
    std::vector<std::string> categories;
    std::vector<LookupHit> hits, words, phrases;
    bool failed = false;
};
enum class Mode { Hidden, Interactive, Pinned };

class Panel {
public:
    explicit Panel(Config config,std::shared_ptr<bilingual::RuntimeStatus> health) :
        config_(std::move(config)),health_(std::move(health)),opacity_(config_.opacity),theme_(config_.theme),draft_theme_(theme_),
        pin_right_(config_.pin_right),
        notes_(utf8((std::filesystem::path(wide_path(config_.database)).parent_path()/L"notes.sqlite3").wstring())) {
        worker_ = std::thread([this] { work(); });
        submit({JobKind::Categories, 1});
        search();
    }
    ~Panel() {
        { std::lock_guard<std::mutex> lock(mutex_); stop_ = true;
          std::erase_if(pending_, [](const Job& job) { return job.kind != JobKind::SaveSide && job.kind != JobKind::SaveAppearance; }); }
        wake_.notify_one();
        if (worker_.joinable()) worker_.join();
    }

    // ReShade calls this before runtime::on_present and ImGui::NewFrame. The
    // IO pointer belongs to the captured runtime, not the ambient ImGui context.
    void before_present(reshade::api::swapchain* swapchain) {
        if (!fallback_io_ || !swapchain || fallback_native_ != swapchain->get_native() ||
            fallback_thread_ != GetCurrentThreadId()) return;
        if (swapchain->get_hwnd()) {
            if (window_input_.mode()!=static_cast<int>(Mode::Interactive) || !window_input_.has_focus()) return;
            // This runtime also supplies sampled input between present and NewFrame.
            // Use the window's queued edges for this frame, then restore the host's
            // normal event intake in its overlay callback. The add-on ABI exposes IO fields.
            if (!restore_events_) {
                prior_events_=fallback_io_->AppAcceptingEvents;
                fallback_io_->AppAcceptingEvents=false;
                restore_events_=true;
            }
        }
        window_input_.feed(*fallback_io_);
    }
    void runtime_destroyed(reshade::api::effect_runtime* runtime) {
        notes_.flush();
        if (fallback_runtime_ == runtime) {
            restore_host_events();
            fallback_io_ = nullptr;
            fallback_runtime_ = nullptr;
            fallback_native_ = 0;
        }
    }
    void report_exception(const char* detail,reshade::api::effect_runtime* runtime=nullptr) noexcept {
        if (exception_logged_) return;
        exception_logged_ = true;
        try {
            log_event(config_,(std::string("Overlay callback exception: ")+detail).c_str());
            if(health_) health_->runtime_error(runtime?runtime:fallback_runtime_,"render_error",detail);
        } catch (...) {}
    }

    void render(reshade::api::effect_runtime* runtime) {
        if (runtime==fallback_runtime_) restore_host_events();
        accept_replies();
        auto& io = ImGui::GetIO();
        const bool fallback = runtime->get_hwnd() == nullptr || config_.game_id == "lis-remastered";
        if (!overlay_logged_) {
            overlay_logged_ = true;
            log_event(config_,fallback?"First overlay callback; using game window input":"First overlay callback; using ReShade input");
        }
        if (fallback) {
            if (fallback_runtime_ && fallback_runtime_ != runtime) return;
            fallback_runtime_ = runtime;
            fallback_native_ = runtime->get_native();
            fallback_io_ = &io;
            fallback_thread_ = GetCurrentThreadId();
            const int bound = window_input_.ensure(static_cast<HWND>(runtime->get_hwnd()));
            if (bound > 0) {
                std::ostringstream detail;
                detail << "Game window input bound HWND=" << window_input_.window()
                       << " PID=" << GetCurrentProcessId();
                log_event(config_,detail.str().c_str());
                window_input_.set_mode(static_cast<int>(mode_));
            } else if (bound < 0 && !bind_failure_logged_) {
                bind_failure_logged_ = true;
                log_event(config_,"Game window input unavailable; will retry");
            }
            const Mode requested = static_cast<Mode>(window_input_.mode());
            if (requested != mode_) {
                mode_ = requested;
                focus_search_ = mode_ == Mode::Interactive;
            }
            if (const unsigned toggles=window_input_.take_toggles()) {
                log_event(config_,mode_==Mode::Interactive?"F8 transition: interactive (window input fallback)":"F8 transition: hidden (window input fallback)");
            }
        } else {
            // Existing ReShade input remains unchanged for ordinary HWND chains.
            if (runtime->is_key_pressed(VK_F8)) {
                mode_ = mode_ == Mode::Interactive ? Mode::Hidden : Mode::Interactive;
                focus_search_ = mode_ == Mode::Interactive;
                log_event(config_,mode_==Mode::Interactive?"F8 transition: interactive (ReShade input)":"F8 transition: hidden (ReShade input)");
            }
            if (mode_ == Mode::Interactive) {
                if (runtime->is_key_pressed(VK_ESCAPE)) mode_ = Mode::Hidden;
            }
        }
        const HWND input_window=fallback?window_input_.window():static_cast<HWND>(runtime->get_hwnd());
        DWORD input_process=0;
        if(input_window) GetWindowThreadProcessId(input_window,&input_process);
        const bool input_ready=input_window&&IsWindow(input_window)&&input_process==GetCurrentProcessId();
        health_update([&](auto& status){status.overlay(runtime,input_ready,false);});
        io.MouseDrawCursor = mode_ == Mode::Interactive && (!fallback || window_input_.has_focus());
        if (mode_ == Mode::Hidden) return;

        if (pending_search_ && std::chrono::steady_clock::now() >= search_at_) { pending_search_ = false; search(); }
        apply_theme();
        const ImVec2 screen = io.DisplaySize;
        if (mode_ == Mode::Pinned) {
            const bool drawn = render_pinned(screen);
            restore_theme();
            health_update([&](auto& status){status.overlay(runtime,input_ready,drawn);});
            return;
        }
        const float width = std::min(1220.0f, screen.x - 32.0f);
        const float height = std::min(790.0f, screen.y - 40.0f);
        ImGui::SetNextWindowSize(ImVec2(width, height), ImGuiCond_FirstUseEver);
        ImGui::SetNextWindowPos(ImVec2(screen.x*0.5f, screen.y*0.5f), ImGuiCond_FirstUseEver, ImVec2(0.5f,0.5f));
        ImGui::SetNextWindowSizeConstraints(ImVec2(std::min(840.0f,width),std::min(520.0f,height)), ImVec2(std::max(840.0f,screen.x-16),std::max(520.0f,screen.y-16)));
        ImGui::SetNextWindowBgAlpha(opacity_);
        ImGuiWindowFlags flags = ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoTitleBar;
        const bool panel_drawn=ImGui::Begin("BilingualWorkbenchPanel", nullptr, flags);
        if (panel_drawn) {
            render_toolbar();
            ImGui::Separator();
            ImGui::Spacing();
            const float footer_height = ImGui::GetFrameHeightWithSpacing() + 4.0f;
            if (ImGui::BeginTable("columns",2,ImGuiTableFlags_Resizable|ImGuiTableFlags_SizingFixedFit)) {
                ImGui::TableSetupColumn("search",ImGuiTableColumnFlags_WidthFixed,std::clamp(ImGui::GetWindowWidth()*.29f,210.f,345.f));
                ImGui::TableSetupColumn("reading",ImGuiTableColumnFlags_WidthStretch);
                ImGui::TableNextRow();
                ImGui::TableSetColumnIndex(0);
                const float body_height = std::max(220.0f,ImGui::GetContentRegionAvail().y-footer_height);
                render_search(body_height);
                ImGui::TableSetColumnIndex(1);
                render_reading(body_height);
                ImGui::EndTable();
            }
            render_status();
        }
        ImGui::End();
        restore_theme();
        if (mode_ != Mode::Interactive && fallback) window_input_.set_mode(static_cast<int>(mode_));
        if (mode_ == Mode::Interactive && !fallback) runtime->block_input_next_frame();
        health_update([&](auto& status){status.overlay(runtime,input_ready,panel_drawn);});
    }

private:
    void restore_host_events() {
        if (restore_events_ && fallback_io_) fallback_io_->AppAcceptingEvents=prior_events_;
        restore_events_=false;
    }
    template<class F> void health_update(F&& update) noexcept {
        try {if(health_) update(*health_);} catch(...) {
            OutputDebugStringW(L"BilingualPanel: health snapshot update failed.\n");
        }
    }
    Config config_;
    std::shared_ptr<bilingual::RuntimeStatus> health_;
    Mode mode_ = Mode::Hidden;
    bilingual::WindowInput window_input_;
    ImGuiIO* fallback_io_ = nullptr;
    bool prior_events_ = true, restore_events_ = false;
    reshade::api::effect_runtime* fallback_runtime_ = nullptr;
    uint64_t fallback_native_ = 0;
    DWORD fallback_thread_ = 0;
    bool overlay_logged_ = false, bind_failure_logged_ = false, exception_logged_ = false;
    float opacity_;
    Theme theme_,draft_theme_;
    bool pin_right_, show_translation_ = true, lookup_failed_ = false;
    std::string entry_en_, entry_zh_;
    struct CardLine { std::string text; float height = 0; };
    std::array<CardLine,4> card_lines_;
    std::vector<std::pair<CardLine,CardLine>> result_previews_;
    float preview_width_ = 0, preview_font_size_ = 0;
    bool card_dirty_ = true;
    ImVec2 card_screen_{};
    float card_font_size_ = 0;
    bilingual::notes::NoteWriter notes_;
    std::deque<Receipt> failed_notes_;
    std::string receipt_entry_, receipt_target_, receipt_text_;
    Clock::time_point receipt_until_{};
    bool draft_pending_=false,saving_appearance_=false;
    std::uint64_t appearance_revision_=0,saved_appearance_revision_=0,save_generation_=0;
    std::string appearance_feedback_;
    std::thread worker_;
    std::mutex mutex_;
    std::condition_variable wake_;
    bool stop_ = false;
    std::deque<Job> pending_;
    std::deque<Reply> replies_;
    std::uint64_t search_generation_ = 0, entry_generation_ = 0, lookup_generation_ = 0;
    std::vector<std::string> categories_;
    SearchPage page_;
    std::optional<Entry> entry_;
    std::vector<LookupHit> words_, phrases_, definitions_;
    std::array<char,512> query_{}, lookup_{};
    std::string category_, selected_id_, selected_text_, status_ = "正在读取本地词句库…";
    int selection_anchor_ = -1, selection_end_ = -1;
    bool searching_ = false, loading_entry_ = false, looking_up_ = false, focus_search_ = false;
    bool pending_search_ = false;
    std::chrono::steady_clock::time_point search_at_{};

    void submit(Job job) {
        { std::lock_guard<std::mutex> lock(mutex_);
          pending_.erase(std::remove_if(pending_.begin(),pending_.end(),[&](const Job& old){return old.kind==job.kind;}),pending_.end());
          pending_.push_back(std::move(job)); }
        wake_.notify_one();
    }
    void search(std::int64_t offset = 0) {
        pending_search_ = false;
        searching_ = true;
        submit({JobKind::Search,++search_generation_,trim(query_.data()),category_,offset});
    }
    void select_entry(const std::string& id) {
        if (id == selected_id_) return;
        selected_id_ = id;
        entry_.reset(); words_.clear(); phrases_.clear(); definitions_.clear();
        selected_text_.clear(); selection_anchor_ = selection_end_ = -1;
        lookup_failed_ = false;
        lookup_[0] = '\0'; ++lookup_generation_; looking_up_ = false;
        loading_entry_ = true;
        submit({JobKind::Entry,++entry_generation_,id});
    }
    void lookup(const std::string& term, bool from_sentence=false) {
        const auto clean = trim(term);
        if (clean.empty()) return;
        selected_text_ = clean;
        if (!from_sentence) selection_anchor_ = selection_end_ = -1;
        std::copy_n(clean.c_str(),std::min(clean.size(),lookup_.size()-1),lookup_.data());
        lookup_[std::min(clean.size(),lookup_.size()-1)] = '\0';
        ++lookup_generation_; definitions_.clear(); looking_up_ = false; lookup_failed_ = false;
        const auto key = bilingual::data::normalize_term(clean);
        for (const auto& hit : phrases_) {
            if (hit.entry_annotation && bilingual::data::normalize_term(hit.term) == key) {
                definitions_.push_back(hit);
                return;
            }
        }
        looking_up_ = true;
        submit({JobKind::Lookup,lookup_generation_,clean});
    }
    void work() noexcept {
        std::unique_ptr<Corpus> corpus;
        std::string corpus_failure;
        try { corpus = std::make_unique<Corpus>(config_.database,config_.game_id); log_event(config_,"Corpus opened read-only"); }
        catch (const std::exception& error) {
            corpus_failure=error.what();log_event(config_,("Corpus open failed: "+corpus_failure).c_str());
            health_update([&](auto& status){status.error("db_error",corpus_failure,true);});
        } catch (...) {
            corpus_failure="Unknown exception while opening the local corpus.";log_event(config_,"Corpus open failed");
            health_update([&](auto& status){status.error("db_error",corpus_failure,true);});
        }
        for (;;) {
            Job job;
            { std::unique_lock<std::mutex> lock(mutex_);
              wake_.wait(lock,[this]{return stop_||!pending_.empty();});
              if (pending_.empty()) return;
              job = std::move(pending_.front()); pending_.pop_front(); }
            Reply reply; reply.job = job;
            try {
                if (!corpus && job.kind!=JobKind::SaveAppearance && job.kind!=JobKind::SaveSide) throw std::runtime_error(corpus_failure.empty()?"Corpus unavailable":corpus_failure);
                switch (job.kind) {
                case JobKind::Categories: reply.categories = corpus->categories(); break;
                case JobKind::Search:
                    reply.page = corpus->search(SearchRequest{job.value,job.category,kPageSize,job.offset});
                    if(job.value.empty()&&job.category.empty())
                        health_update([&](auto& status){status.corpus_count(reply.page.total);});
                    break;
                case JobKind::Entry:
                    reply.entry = corpus->entry(job.value);
                    if (reply.entry) {
                        reply.words = corpus->sentenceWords(reply.entry->en);
                        reply.phrases = corpus->entryPhrases(*reply.entry);
                    }
                    break;
                case JobKind::Lookup: reply.hits = corpus->lookup(job.value); break;
                case JobKind::SaveAppearance: save_panel_settings(config_,job.theme,job.opacity,std::nullopt);break;
                case JobKind::SaveSide: save_panel_settings(config_,job.theme,job.opacity,job.pin_right);break;
                }
                if(job.kind!=JobKind::SaveAppearance && job.kind!=JobKind::SaveSide) health_update([](auto& status){status.query_succeeded();});
                else health_update([](auto& status){status.configuration_saved();});
            } catch (const std::exception& error) {
                reply.failed=true;
                log_event(config_,("Background operation failed: "+std::string(error.what())).c_str());
                health_update([&](auto& status){
                    const bool database=!corpus||job.kind==JobKind::Categories||job.kind==JobKind::Search;
                    status.error((job.kind==JobKind::SaveAppearance || job.kind==JobKind::SaveSide)?"config_write_error":database?"db_error":"query_error",error.what(),database);
                });
            } catch (...) {
                reply.failed=true;
                if(job.kind!=JobKind::SaveAppearance) log_event(config_,"Background query failed");
                health_update([&](auto& status){status.error(job.kind==JobKind::SaveAppearance?"config_write_error":"db_error","Unknown background operation exception.",job.kind!=JobKind::SaveAppearance);});
            }
            { std::lock_guard<std::mutex> lock(mutex_); if (!stop_) replies_.push_back(std::move(reply)); }
        }
    }
    void accept_replies() {
        for (auto& receipt : notes_.take_receipts()) {
            std::erase_if(failed_notes_, [&](const Receipt& old) {
                return old.note.entry_id == receipt.note.entry_id && old.note.target_key == receipt.note.target_key;
            });
            if (!receipt.error.empty()) { failed_notes_.push_back(std::move(receipt)); continue; }
            receipt_entry_ = receipt.note.entry_id;
            receipt_target_ = receipt.note.target_key;
            receipt_text_ = receipt.inserted ? "已记下" : "已记过";
            receipt_until_ = Clock::now() + std::chrono::seconds(2);
        }
        std::deque<Reply> ready;
        { std::lock_guard<std::mutex> lock(mutex_); ready.swap(replies_); }
        for (auto& reply : ready) {
            const Job& job = reply.job;
            if (job.kind==JobKind::Search && job.generation!=search_generation_) continue;
            if (job.kind==JobKind::Entry && job.generation!=entry_generation_) continue;
            if (job.kind==JobKind::Lookup && job.generation!=lookup_generation_) continue;
            if (job.kind==JobKind::SaveAppearance && job.generation!=save_generation_) continue;
            if (reply.failed) {
                if(job.kind==JobKind::SaveSide) { status_="小卡位置保存失败，请检查游戏目录权限后重新选择。"; continue; }
                if(job.kind==JobKind::SaveAppearance) {
                    saving_appearance_=false;
                    appearance_feedback_="保存失败，请检查游戏目录的写入权限；当前预览仍保留。";
                    continue;
                }
                status_ = "本地资料暂时无法读取；请检查工作台索引与面板配置。";
                if (job.kind==JobKind::Search) searching_=false;
                if (job.kind==JobKind::Entry) loading_entry_=false;
                if (job.kind==JobKind::Lookup) { looking_up_=false; lookup_failed_=true; card_dirty_=true; }
                continue;
            }
            if (job.kind==JobKind::Categories) categories_ = std::move(reply.categories);
            else if (job.kind==JobKind::Search) {
                page_ = std::move(reply.page); searching_=false;
                result_previews_.clear();
                status_ = page_.tokens_truncated ? "本次检索使用前 12 个关键词。" : "点击英文查词   ·   按住 Shift 点末词选词组   ·   F8 返回游戏";
                if (!page_.items.empty()) {
                    const auto selected = std::find_if(page_.items.begin(),page_.items.end(),
                        [&](const Entry& row) { return row.id == selected_id_; });
                    if (selected == page_.items.end()) select_entry(page_.items.front().id);
                }
                else { selected_id_.clear(); entry_.reset(); words_.clear(); phrases_.clear(); ++entry_generation_; loading_entry_=false; }
            } else if (job.kind==JobKind::Entry) {
                entry_ = std::move(reply.entry); words_=std::move(reply.words); phrases_=std::move(reply.phrases); loading_entry_=false;
                entry_en_=entry_ ? display_text(entry_->en) : "";
                entry_zh_=entry_ ? display_text(entry_->zh) : "";
                card_dirty_=true;
            } else if(job.kind==JobKind::Lookup) { definitions_=std::move(reply.hits); looking_up_=false; card_dirty_=true; }
            else if(job.kind==JobKind::SaveAppearance) {
                saving_appearance_=false;saved_appearance_revision_=job.appearance_revision;
                appearance_feedback_="已保存到当前游戏，下次启动沿用此外观。";
            }
        }
    }
    void apply_draft() {
        const bool adjusted=protect_contrast(draft_theme_);
        theme_=draft_theme_;draft_pending_=false;++appearance_revision_;
        appearance_feedback_=adjusted?"已调整文字与底色，保证文字清晰。":"已应用预览；保存后下次启动继续使用。";
    }
    void request_save_appearance() {
        if(draft_pending_) apply_draft();
        Job job;job.kind=JobKind::SaveAppearance;job.generation=++save_generation_;
        job.theme=theme_;job.opacity=opacity_;job.appearance_revision=appearance_revision_;
        saving_appearance_=true;appearance_feedback_="正在保存当前游戏外观…";submit(std::move(job));
    }
    void render_toolbar() {
        const auto top=ImGui::GetCursorScreenPos();
        const float available=ImGui::GetContentRegionAvail().x;
        auto* draw=ImGui::GetWindowDrawList();
        const auto accent=ImGui::GetColorU32(theme_.accent);
        draw->AddRectFilled(ImVec2(top.x,top.y+3),ImVec2(top.x+33,top.y+36),
                            ImGui::GetColorU32(blend(theme_.panel,theme_.accent,.16f)),8);
        draw->AddRect(ImVec2(top.x+8,top.y+10),ImVec2(top.x+25,top.y+28),accent,2,0,1.8f);
        draw->AddLine(ImVec2(top.x+13,top.y+10),ImVec2(top.x+13,top.y+28),accent,1.6f);
        draw->AddLine(ImVec2(top.x+16,top.y+16),ImVec2(top.x+22,top.y+16),accent,1.5f);
        ImGui::Dummy(ImVec2(33,38));
        ImGui::SameLine(0,12);
        ImGui::PushFont(nullptr,29.0f);
        ImGui::AlignTextToFramePadding();
        ImGui::TextUnformatted("双语阅读");
        ImGui::PopFont();
        ImGui::SameLine(0,16);
        ImGui::AlignTextToFramePadding();
        const float game_width=std::min(available*.32f,ImGui::CalcTextSize(config_.title.c_str()).x);
        const auto title_pos=ImGui::GetCursorScreenPos();
        draw->PushClipRect(title_pos,ImVec2(title_pos.x+game_width,title_pos.y+ImGui::GetFrameHeight()),true);
        draw->AddText(ImVec2(title_pos.x,title_pos.y+ImGui::GetStyle().FramePadding.y),
                      ImGui::GetColorU32(theme_.muted),config_.title.c_str());
        draw->PopClipRect();
        ImGui::Dummy(ImVec2(game_width,ImGui::GetFrameHeight()));
        const float space=ImGui::GetStyle().ItemSpacing.x;
        const float actions=132.f+96.f+108.f+2*space;
        const float right=top.x+available-actions;
        if (right>ImGui::GetItemRectMax().x+space) {
            ImGui::SameLine();
            ImGui::SetCursorScreenPos(ImVec2(right,top.y));
        }
        ImGui::BeginDisabled(!entry_ || pending_search_ || searching_ || loading_entry_);
        if (ImGui::Button("固定阅读",ImVec2(132,0))) {
            mode_=Mode::Pinned;
            card_dirty_=true;
            ImGui::GetIO().MouseDrawCursor=false;
        }
        ImGui::EndDisabled();
        ImGui::SameLine();
        const bool draft=appearance_revision_!=saved_appearance_revision_ || draft_pending_;
        if (ImGui::Button(draft ? "外观 *###appearance_button" : "外观###appearance_button",ImVec2(96,0)))
            ImGui::OpenPopup("appearance");
        render_appearance();
        ImGui::SameLine();
        if (ImGui::Button("收起  Esc",ImVec2(108,0))) {
            mode_=Mode::Hidden;
            ImGui::GetIO().MouseDrawCursor=false;
        }
    }

    void render_appearance() {
        if(!ImGui::IsPopupOpen("appearance")) return;
        ImGui::SetNextWindowSizeConstraints(ImVec2(420,0),ImVec2(620,1000));
        if(!ImGui::BeginPopup("appearance")) return;
        ImGui::TextColored(theme_.accent,"当前游戏外观");
        ImGui::TextUnformatted("小卡位置");
        ImGui::SameLine();
        const bool left=ImGui::RadioButton("靠左",!pin_right_);
        ImGui::SameLine();
        const bool right=ImGui::RadioButton("靠右",pin_right_);
        if ((left && pin_right_) || (right && !pin_right_)) {
            pin_right_=right;
            Job job; job.kind=JobKind::SaveSide; job.pin_right=right;
            submit(std::move(job));
        }
        ImGui::SetNextItemWidth(240);
        if(ImGui::BeginCombo("预设",draft_theme_.name.c_str())) {
            for(int i=0;i<3;++i) if(ImGui::Selectable(kPresetNames[i],draft_theme_.source==std::string("preset:")+kPresetIds[i])) {
                draft_theme_=preset_theme(i);apply_draft();
            }
            ImGui::EndCombo();
        }
        if(ImGui::CollapsingHeader("自定义颜色")) {
            for(std::size_t i=0;i<kThemeKeys.size();++i) {
                ImGui::SetNextItemWidth(250);
                if(ImGui::ColorEdit3(kThemeLabels[i],&theme_color(draft_theme_,kThemeKeys[i])->x,
                    ImGuiColorEditFlags_DisplayHex|ImGuiColorEditFlags_NoAlpha|ImGuiColorEditFlags_NoSidePreview)) {
                    draft_theme_.name="自定义";draft_theme_.source="game-panel";draft_pending_=true;
                }
            }
            ImGui::BeginDisabled(!draft_pending_);
            if(ImGui::Button("应用预览")) apply_draft();
            ImGui::EndDisabled();
        }
        ImGui::SetNextItemWidth(240);
        if(ImGui::SliderFloat("背景不透明度",&opacity_,0.35f,0.95f,"%.2f",ImGuiSliderFlags_AlwaysClamp)) ++appearance_revision_;
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX()+430);
        ImGui::TextColored(theme_.muted,"仅改变背景透明度，文字保持不透明。颜色自动保护阅读对比度。");
        ImGui::BeginDisabled(saving_appearance_);
        if(ImGui::Button(saving_appearance_?"正在保存…":"保存外观")) request_save_appearance();
        ImGui::EndDisabled();
        ImGui::SameLine();if(ImGui::Button("关闭")) ImGui::CloseCurrentPopup();
        if(!appearance_feedback_.empty()) ImGui::TextColored(theme_.muted,"%s",appearance_feedback_.c_str());
        ImGui::PopTextWrapPos();ImGui::EndPopup();
    }
    void apply_theme() {
        ImGui::PushFont(nullptr,20.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_Alpha,1.0f);
        ImGui::PushStyleColor(ImGuiCol_Text,theme_.text);
        ImGui::PushStyleColor(ImGuiCol_TextDisabled,theme_.muted);
        ImGui::PushStyleColor(ImGuiCol_WindowBg,theme_.background);
        ImGui::PushStyleColor(ImGuiCol_ChildBg,ImVec4(0,0,0,0));
        ImGui::PushStyleColor(ImGuiCol_PopupBg,blend(theme_.panel,theme_.panel,0,.98f));
        ImGui::PushStyleColor(ImGuiCol_Border,blend(theme_.border,theme_.border,0,.65f));
        ImGui::PushStyleColor(ImGuiCol_FrameBg,blend(theme_.panel,theme_.panel,0,.82f));
        ImGui::PushStyleColor(ImGuiCol_FrameBgHovered,blend(theme_.panel,theme_.accent,.13f,.9f));
        ImGui::PushStyleColor(ImGuiCol_FrameBgActive,blend(theme_.panel,theme_.accent,.19f,.95f));
        ImGui::PushStyleColor(ImGuiCol_Button,blend(theme_.panel,theme_.accent,.12f,.85f));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,blend(theme_.panel,theme_.accent,.20f,.95f));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,blend(theme_.panel,theme_.accent,.25f));
        ImGui::PushStyleColor(ImGuiCol_Header,blend(theme_.panel,theme_.accent,.19f,.8f));
        ImGui::PushStyleColor(ImGuiCol_HeaderHovered,blend(theme_.panel,theme_.accent,.16f,.65f));
        ImGui::PushStyleColor(ImGuiCol_HeaderActive,blend(theme_.panel,theme_.accent,.23f,.9f));
        ImGui::PushStyleColor(ImGuiCol_CheckMark,theme_.accent);
        ImGui::PushStyleColor(ImGuiCol_SliderGrab,theme_.accent);
        ImGui::PushStyleColor(ImGuiCol_SliderGrabActive,theme_.text);
        ImGui::PushStyleColor(ImGuiCol_Separator,blend(theme_.border,theme_.border,0,.5f));
        ImGui::PushStyleColor(ImGuiCol_Tab,blend(theme_.panel,theme_.accent,.04f,.8f));
        ImGui::PushStyleColor(ImGuiCol_TabHovered,blend(theme_.panel,theme_.accent,.16f,.9f));
        ImGui::PushStyleColor(ImGuiCol_TabSelected,blend(theme_.panel,theme_.accent,.18f,.9f));
        ImGui::PushStyleColor(ImGuiCol_TabSelectedOverline,theme_.accent);
        ImGui::PushStyleColor(ImGuiCol_ScrollbarBg,blend(theme_.background,theme_.background,0,.2f));
        ImGui::PushStyleColor(ImGuiCol_ScrollbarGrab,blend(theme_.border,theme_.accent,.2f,.7f));
        ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding,14.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding,9.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding,6.0f);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,ImVec2(24,20));
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,ImVec2(10,7));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,ImVec2(10,10));
        ImGui::PushStyleVar(ImGuiStyleVar_CellPadding,ImVec2(10,0));
    }
    static void restore_theme() { ImGui::PopStyleVar(8); ImGui::PopStyleColor(25); ImGui::PopFont(); }
    void render_search(float height) {
        ImGui::BeginChild("search_region",ImVec2(0,height),ImGuiChildFlags_None,ImGuiWindowFlags_NoScrollbar);
        ImGui::PushFont(nullptr,23.0f);
        ImGui::TextUnformatted("查找原句");
        ImGui::PopFont();
        ImGui::SetNextItemWidth(-1);
        if (focus_search_) { ImGui::SetKeyboardFocusHere(); focus_search_=false; }
        if (ImGui::InputTextWithHint("##query","输入英文、中文或片段",query_.data(),query_.size())) {
            pending_search_=true; search_at_=std::chrono::steady_clock::now()+std::chrono::milliseconds(220);
        }
        ImGui::SetNextItemWidth(-92);
        if (ImGui::BeginCombo("##category",category_.empty()?"全部类别":category_.c_str())) {
            if (ImGui::Selectable("全部类别",category_.empty())) { category_.clear(); search(); }
            for (const auto& category:categories_) if (ImGui::Selectable(category.c_str(),category_==category)) { category_=category; search(); }
            ImGui::EndCombo();
        }
        ImGui::SameLine();
        ImGui::BeginDisabled(query_[0]=='\0' && category_.empty());
        if (ImGui::Button("清空",ImVec2(72,0))) { query_[0]='\0'; category_.clear(); search(); }
        ImGui::EndDisabled();
        if (searching_) ImGui::TextColored(theme_.muted,"正在检索…");
        else ImGui::TextColored(theme_.muted,"%lld 条原句",static_cast<long long>(page_.total));
        const float list_height=std::max(80.0f,ImGui::GetContentRegionAvail().y-ImGui::GetFrameHeightWithSpacing()-6);
        ImGui::BeginChild("results",ImVec2(0,list_height));
        if (!searching_ && page_.items.empty()) { ImGui::TextWrapped("没有找到匹配原句。试试更短的片段，或切换到全部类别。"); }
        const float available=std::max(60.0f,ImGui::GetContentRegionAvail().x-28);
        if (preview_width_!=available || preview_font_size_!=ImGui::GetFontSize() || result_previews_.size()!=page_.items.size()) {
            preview_width_=available;
            preview_font_size_=ImGui::GetFontSize();
            result_previews_.clear();
            auto preview=[&](const std::string& raw,int lines) {
                CardLine line;
                line.text=fit_card_text(display_text(raw),available,lines);
                line.height=ImGui::CalcTextSize(line.text.c_str(),nullptr,false,available).y;
                return line;
            };
            for (const auto& item:page_.items)
                result_previews_.emplace_back(preview(item.en,3),preview(item.zh,2));
        }
        for (std::size_t index=0;index<page_.items.size();++index) {
            const auto& row=page_.items[index];
            ImGui::PushID(row.id.c_str());
            const auto& [english,chinese]=result_previews_[index];
            const float en_height=english.height, zh_height=chinese.height;
            const float card_height=en_height+zh_height+50;
            const auto start=ImGui::GetCursorScreenPos();
            const bool selected=selected_id_==row.id;
            if (ImGui::Selectable("##card",selected,ImGuiSelectableFlags_None,ImVec2(0,card_height))) select_entry(row.id);
            auto* draw=ImGui::GetWindowDrawList();
            if (selected)
                draw->AddRectFilled(ImVec2(start.x,start.y+5),ImVec2(start.x+3,start.y+card_height-5),
                                    ImGui::GetColorU32(theme_.accent),1.5f);
            draw->PushClipRect(ImVec2(start.x+10,start.y+7),ImVec2(start.x+available+10,start.y+card_height-5),true);
            draw->AddText(ImVec2(start.x+10,start.y+7),ImGui::GetColorU32(theme_.muted),row.category.c_str());
            draw->PushClipRect(ImVec2(start.x+10,start.y+34),ImVec2(start.x+available+10,start.y+34+en_height),true);
            draw->AddText(ImGui::GetFont(),ImGui::GetFontSize(),ImVec2(start.x+10,start.y+34),ImGui::GetColorU32(theme_.text),english.text.c_str(),nullptr,available);
            draw->PopClipRect();
            draw->PushClipRect(ImVec2(start.x+10,start.y+34+en_height+7),ImVec2(start.x+available+10,start.y+34+en_height+7+zh_height),true);
            draw->AddText(ImGui::GetFont(),ImGui::GetFontSize(),ImVec2(start.x+10,start.y+34+en_height+7),ImGui::GetColorU32(theme_.muted),chinese.text.c_str(),nullptr,available);
            draw->PopClipRect();
            draw->PopClipRect();
            ImGui::PopID();
        }
        ImGui::EndChild();
        ImGui::BeginDisabled(searching_||page_.offset<=0);
        if (ImGui::Button("上一页")) search(std::max<std::int64_t>(0,page_.offset-kPageSize));
        ImGui::EndDisabled(); ImGui::SameLine();
        ImGui::TextColored(theme_.muted,"%lld / %lld",static_cast<long long>(page_.total ? page_.offset/kPageSize+1:0),static_cast<long long>((page_.total+kPageSize-1)/kPageSize));
        ImGui::SameLine();
        ImGui::BeginDisabled(searching_||page_.offset+kPageSize>=page_.total);
        if (ImGui::Button("下一页")) search(page_.offset+kPageSize);
        ImGui::EndDisabled();
        ImGui::EndChild();
    }
    static const char* origin_label(const LookupHit& hit) {
        return hit.entry_annotation ? "原句注释" : "词典释义";
    }

    static std::string meaning_source(const LookupHit& hit) {
        std::string source = origin_label(hit);
        if (hit.is_candidate) source += " · 候选词形 " + hit.term;
        if (!hit.source.empty()) source += " · " + hit.source;
        return source;
    }

    void save_note(bool whole) {
        Note note;
        note.game_id = config_.game_id;
        note.game_title = config_.title;
        note.entry_id = entry_->id;
        note.source_key = entry_->key;
        note.name_space = entry_->name_space;
        note.entry_source = entry_->source;
        note.en = entry_->en;
        note.zh = entry_->zh;
        note.target = whole ? "" : selected_text_;
        note.target_key = bilingual::data::normalize_term(note.target);
        note.kind = whole ? "sentence" : note.target_key.find(' ') == std::string::npos ? "word" : "phrase";
        note.saved_at_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::system_clock::now().time_since_epoch()).count();
        if (!definitions_.empty()) {
            const auto& hit = definitions_.front();
            note.meaning = whole ? selected_text_ + " · " + hit.meaning : hit.meaning;
            note.meaning_source = meaning_source(hit);
        } else if (!whole) {
            note.meaning = "本地词典暂无对应释义";
            note.meaning_source = "未收录";
        } else {
            for (const auto& hit : phrases_) {
                if (!hit.entry_annotation) continue;
                if (!note.meaning.empty()) { note.meaning += "\n"; note.meaning_source += "\n"; }
                note.meaning += hit.term + " · " + hit.meaning;
                note.meaning_source += meaning_source(hit);
            }
            if (note.meaning.empty() && !entry_->note.empty()) {
                note.meaning = entry_->note;
                note.meaning_source = "原句注释";
            }
        }
        notes_.submit(std::move(note));
    }

    bool recent_receipt(const std::string& target) const {
        return Clock::now() < receipt_until_ && receipt_entry_ == selected_id_ &&
               receipt_target_ == bilingual::data::normalize_term(target);
    }

    void render_capture_buttons() {
        const bool whole = selected_text_.empty();
        const char* label = recent_receipt(selected_text_) ? receipt_text_.c_str() : "记到笔记";
        ImGui::BeginDisabled(!entry_ || loading_entry_ || looking_up_ || lookup_failed_);
        const auto ink=contrast(theme_.accent,rgb(0x121814)) >= contrast(theme_.accent,rgb(0xffffff))
            ? rgb(0x121814) : rgb(0xffffff);
        ImGui::PushStyleColor(ImGuiCol_Text,ink);
        ImGui::PushStyleColor(ImGuiCol_Button,theme_.accent);
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered,blend(theme_.accent,ink,.07f));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive,blend(theme_.accent,ink,.13f));
        if (ImGui::Button((std::string(label) + "###save_target").c_str(),ImVec2(132,0))) save_note(whole);
        ImGui::PopStyleColor(4);
        ImGui::EndDisabled();
        if (!whole) {
            ImGui::SameLine();
            if (ImGui::Button(recent_receipt("") ? (receipt_text_+"###save_sentence").c_str()
                                                 : "另记整句###save_sentence")) save_note(true);
        }
        ImGui::SameLine();
        ImGui::PushTextWrapPos(0);
        ImGui::TextColored(theme_.muted,"%s",whole ? "整句" : selected_text_.c_str());
        ImGui::PopTextWrapPos();
    }

    void render_status() {
        if (failed_notes_.empty()) { ImGui::TextColored(theme_.muted,"%s",status_.c_str()); return; }
        const auto& failure = failed_notes_.front();
        if (ImGui::SmallButton("重试保存")) {
            notes_.submit(failure.note);
            failed_notes_.pop_front();
            return;
        }
        ImGui::SameLine();
        ImGui::TextWrapped("未记下「%s」 · %s", failure.note.target.empty() ? "整句" : failure.note.target.c_str(),
                           failure.error.c_str());
    }

    void render_gloss() {
        if (selected_text_.empty()) return;
        ImGui::PushStyleColor(ImGuiCol_ChildBg,blend(theme_.panel,theme_.accent,.11f,std::min(1.f,opacity_+.08f)));
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,ImVec2(16,11));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,ImVec2(8,5));
        ImGui::BeginChild("selected_gloss",ImVec2(0,0),
            ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysUseWindowPadding,ImGuiWindowFlags_NoScrollbar);
        ImGui::PushFont(nullptr,21.0f);
        ImGui::PushTextWrapPos(0);
        if (looking_up_) ImGui::TextColored(theme_.muted,"正在查词…");
        else if (lookup_failed_) {
            ImGui::TextUnformatted("读取释义失败");
            ImGui::SameLine();
            if (ImGui::SmallButton("重新查词")) lookup(selected_text_,selection_anchor_ >= 0);
        } else if (definitions_.empty()) {
            ImGui::TextColored(theme_.muted,"%s · 本地词典暂无对应释义",selected_text_.c_str());
        } else {
            const auto& hit = definitions_.front();
            ImGui::TextColored(theme_.accent,"%s  ·  %s",selected_text_.c_str(),origin_label(hit));
            ImGui::TextUnformatted(hit.meaning.c_str());
            ImGui::PushFont(nullptr,17.0f);
            ImGui::TextColored(theme_.muted,"%s%s",hit.is_candidate ? "候选词形 · " : "",hit.source.c_str());
            ImGui::PopFont();
        }
        ImGui::PopTextWrapPos();
        ImGui::PopFont();
        ImGui::EndChild();
        const auto from=ImGui::GetItemRectMin(),to=ImGui::GetItemRectMax();
        ImGui::GetWindowDrawList()->AddRectFilled(ImVec2(from.x,from.y+6),ImVec2(from.x+3,to.y-6),
                                                   ImGui::GetColorU32(theme_.accent),1.5f);
        ImGui::PopStyleVar(2);
        ImGui::PopStyleColor();
    }

    void render_reading(float height) {
        ImGui::PushStyleColor(ImGuiCol_ChildBg,blend(theme_.panel,theme_.background,.2f,opacity_*.55f));
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,ImVec2(22,16));
        ImGui::BeginChild("reading_region",ImVec2(0,height),ImGuiChildFlags_Borders);
        ImGui::PopStyleVar();
        ImGui::PopStyleColor();
        if (loading_entry_ || !entry_) {
            ImGui::TextWrapped("%s",loading_entry_ ? "正在读取原句…" : "在左侧选择原句");
        } else {
            render_capture_buttons();
            ImGui::Separator();
            ImGui::BeginChild("sentence_region");
            ImGui::TextColored(theme_.muted,"%s%s%s",entry_->category.c_str(),entry_->speaker.empty()?"":"  ·  ",entry_->speaker.c_str());
            ImGui::PushFont(nullptr,32.0f);
            clickable_english(entry_en_);
            ImGui::PopFont();
            if (selection_anchor_ < 0) render_gloss();
            if (!entry_->zh.empty() && ImGui::SmallButton(show_translation_ ? "收起译文" : "展开译文"))
                show_translation_ = !show_translation_;
            if (show_translation_ && !entry_->zh.empty()) {
                ImGui::PushTextWrapPos(0);
                ImGui::TextColored(blend(theme_.text,theme_.muted,.24f),"%s",entry_zh_.c_str());
                ImGui::PopTextWrapPos();
            }
            const bool has_context = !phrases_.empty() && phrases_.front().entry_annotation;
            if ((has_context || !entry_->note.empty()) &&
                (definitions_.empty() || !definitions_.front().entry_annotation)) {
                ImGui::Spacing();
                ImGui::TextColored(theme_.accent,"原句注释");
                if (has_context) {
                    for (std::size_t i=0;i<phrases_.size() && phrases_[i].entry_annotation;++i)
                        render_hit(phrases_[i],static_cast<int>(i),true);
                } else ImGui::TextWrapped("%s",entry_->note.c_str());
            }
            ImGui::Spacing();
            if (ImGui::CollapsingHeader("查其他词与词组")) {
                ImGui::SetNextItemWidth(-90);
                if (ImGui::InputTextWithHint("##lookup","输入英文词或词组",lookup_.data(),lookup_.size(),ImGuiInputTextFlags_EnterReturnsTrue))
                    lookup(lookup_.data());
                ImGui::SameLine();
                if (ImGui::Button("查词")) lookup(lookup_.data());
                for (std::size_t i=1;i<definitions_.size();++i) render_hit(definitions_[i],static_cast<int>(i),false);
                ImGui::PushID("automatic_phrases");
                for (std::size_t i=0;i<phrases_.size();++i)
                    if (!phrases_[i].entry_annotation) render_hit(phrases_[i],static_cast<int>(i),true);
                ImGui::PopID();
                if (ImGui::TreeNode("句内单词")) {
                    for (std::size_t i=0;i<words_.size();++i) render_hit(words_[i],static_cast<int>(i),true);
                    ImGui::TreePop();
                }
            }
            if (ImGui::CollapsingHeader("来源与原文信息")) {
                ImGui::TextWrapped("%s",entry_->source.c_str());
                ImGui::TextWrapped("%s",entry_->key.c_str());
                if (!entry_->note.empty()) ImGui::TextWrapped("%s",entry_->note.c_str());
            }
            ImGui::EndChild();
        }
        ImGui::EndChild();
    }

    // Measure using the live font and cut at a UTF-8 boundary. The full snapshot stays in the reader.
    static std::string fit_card_text(const std::string& text,float width,int lines) {
        const float limit = lines * ImGui::GetTextLineHeight() + 0.5f;
        auto fits = [&](const std::string& s) { return ImGui::CalcTextSize(s.c_str(),nullptr,false,width).y <= limit; };
        if (text.size() <= 4096 && fits(text)) return text;
        std::size_t lo=0, hi=std::min<std::size_t>(4096,text.size());
        auto excerpt = [&](std::size_t end) {
            while (end && end<text.size() && (static_cast<unsigned char>(text[end])&0xc0)==0x80) --end;
            return text.substr(0,end)+"…";
        };
        while (lo<hi) {
            const auto mid=(lo+hi+1)/2;
            if (fits(excerpt(mid))) lo=mid; else hi=mid-1;
        }
        if (lo>0 && lo<text.size() && std::isalpha(static_cast<unsigned char>(text[lo-1])) &&
            std::isalpha(static_cast<unsigned char>(text[lo]))) {
            const auto space=text.find_last_of(" \r\n\t",lo-1);
            if (space!=std::string::npos && lo-space<=18) lo=space;
        }
        return excerpt(lo);
    }

    void card_line(std::size_t index,const std::string& text,const ImVec4& color,float font_size,int lines) {
        if (text.empty()) return;
        ImGui::PushFont(nullptr,font_size);
        const float width=std::max(50.0f,ImGui::GetContentRegionAvail().x);
        auto& line=card_lines_[index];
        if (card_dirty_) {
            line.text=fit_card_text(text,width,lines);
            line.height=ImGui::CalcTextSize(line.text.c_str(),nullptr,false,width).y;
        }
        auto* draw=ImGui::GetWindowDrawList();
        const auto at=ImGui::GetCursorScreenPos();
        const ImU32 shadow=luminance(color)>.40 ? IM_COL32(0,0,0,200) : IM_COL32(255,255,255,190);
        auto paint=[&](ImVec2 pos,ImU32 ink) {
            draw->AddText(ImGui::GetFont(),ImGui::GetFontSize(),pos,ink,line.text.c_str(),nullptr,width);
        };
        paint(ImVec2(at.x,at.y+1),shadow);
        paint(at,ImGui::GetColorU32(color));
        ImGui::Dummy(ImVec2(width,line.height));
        ImGui::PopFont();
    }

    bool render_pinned(ImVec2 screen) {
        const float width=std::min(std::clamp(screen.x*.27f,310.f,470.f),std::max(150.f,screen.x-24.f));
        const float margin=std::clamp(screen.x*.015f,10.f,28.f);
        ImGui::SetNextWindowPos(ImVec2(pin_right_ ? screen.x-margin-width : margin,screen.y*.145f),ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2(width,0),ImGuiCond_Always);
        ImGui::SetNextWindowBgAlpha(opacity_);
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,ImVec2(21,14));
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,ImVec2(8,6));
        const bool drawn=ImGui::Begin("##pinned_reading_card",nullptr,
            ImGuiWindowFlags_NoDecoration | ImGuiWindowFlags_NoInputs | ImGuiWindowFlags_NoFocusOnAppearing |
            ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_AlwaysAutoResize);
        if (drawn && entry_) {
            if (screen.x!=card_screen_.x || screen.y!=card_screen_.y || ImGui::GetFontSize()!=card_font_size_) {
                card_dirty_=true; card_screen_=screen; card_font_size_=ImGui::GetFontSize();
            }
            const int lines=std::clamp(static_cast<int>(screen.y/270.f),2,5);
            const auto pos=ImGui::GetWindowPos(),size=ImGui::GetWindowSize();
            auto* draw=ImGui::GetWindowDrawList();
            draw->AddRectFilled(ImVec2(pos.x+5,pos.y+14),ImVec2(pos.x+8,pos.y+size.y-14),
                                ImGui::GetColorU32(theme_.accent),1.5f);
            card_line(0,entry_en_,theme_.text,26,lines);
            if (show_translation_) card_line(1,entry_zh_,blend(theme_.text,theme_.muted,.22f),19,std::max(2,lines-1));
            if (!selected_text_.empty()) {
                ImGui::Separator();
                const std::string label=definitions_.empty() ? selected_text_ : selected_text_+"  ·  "+origin_label(definitions_.front());
                card_line(2,label,theme_.accent,20,1);
                const auto gloss=looking_up_ ? "正在查词…" : lookup_failed_ ? "读取释义失败" :
                    definitions_.empty() ? "本地词典暂无对应释义" : definitions_.front().meaning;
                card_line(3,gloss,theme_.text,20,lines);
            }
            ImGui::PushFont(nullptr,17.0f);
            const auto at=ImGui::GetCursorScreenPos(), key=ImGui::CalcTextSize("F8");
            draw->AddRect(ImVec2(at.x,at.y),ImVec2(at.x+key.x+12,at.y+key.y+4),
                          ImGui::GetColorU32(theme_.muted),4);
            draw->AddText(ImVec2(at.x+6,at.y+2),ImGui::GetColorU32(theme_.text),"F8");
            draw->AddText(ImVec2(at.x+key.x+21,at.y+2),ImGui::GetColorU32(theme_.text),"返回检索");
            ImGui::Dummy(ImVec2(1,key.y+4));
            ImGui::PopFont();
            card_dirty_=false;
        }
        ImGui::End();
        ImGui::PopStyleVar(2);
        return drawn;
    }
    void render_hit(const LookupHit& hit,int id,bool clickable) {
        ImGui::PushID(id);
        if (clickable) { if (ImGui::SmallButton(hit.term.c_str())) lookup(hit.term); }
        else ImGui::TextColored(theme_.accent,"%s",hit.term.c_str());
        if (hit.is_candidate) { ImGui::SameLine(); ImGui::TextColored(theme_.muted,"候选词形"); }
        ImGui::PushTextWrapPos(0);
        ImGui::TextUnformatted(hit.meaning.c_str());
        ImGui::PushFont(nullptr,16.0f);
        ImGui::TextColored(theme_.muted,"%s",hit.source.c_str());
        ImGui::PopFont(); ImGui::PopTextWrapPos();
        ImGui::Spacing(); ImGui::PopID();
    }
    void clickable_english(const std::string& text) {
        const auto letter=[](char ch){return (ch>='A'&&ch<='Z')||(ch>='a'&&ch<='z');};
        struct Span {std::size_t start,length;bool word;};
        std::vector<Span> spans;
        for(std::size_t i=0;i<text.size();) {
            const auto start=i;
            const bool word=letter(text[i]);
            if (word) {
                while(i<text.size()) {
                    if (letter(text[i])) { ++i; continue; }
                    const std::size_t joiner=text[i]=='\'' || text[i]=='-' ? 1 : text.compare(i,3,"’")==0 ? 3 : 0;
                    if (!joiner || i+joiner>=text.size() || !letter(text[i+joiner])) break;
                    i+=joiner;
                }
            }
            else { ++i; while(i<text.size() && (static_cast<unsigned char>(text[i])&0xc0)==0x80) ++i; }
            spans.push_back({start,i-start,word});
        }
        const ImVec2 origin=ImGui::GetCursorScreenPos();
        const float available=std::max(80.0f,ImGui::GetContentRegionAvail().x);
        const float line_height=ImGui::GetTextLineHeight()+7;
        float x=0,y=0;
        bool gloss_on_line=false;
        const auto advance_line=[&] {
            x=0; y+=line_height;
            if (gloss_on_line) {
                ImGui::SetCursorScreenPos(ImVec2(origin.x+4,origin.y+y+3));
                render_gloss();
                y=ImGui::GetCursorScreenPos().y-origin.y+3;
                gloss_on_line=false;
            }
        };
        auto* draw=ImGui::GetWindowDrawList();
        for(std::size_t i=0;i<spans.size();++i) {
            const auto& span=spans[i];
            const auto token=text.substr(span.start,span.length);
            if(token=="\r") continue;
            if(token=="\n") {advance_line();continue;}
            const auto size=ImGui::CalcTextSize(token.c_str());
            if(x+size.x>available && x>0) advance_line();
            if(x==0 && token==" ") continue;
            const ImVec2 at(origin.x+x,origin.y+y);
            bool hover=false;
            if(span.word) {
                ImGui::SetCursorScreenPos(at); ImGui::PushID(static_cast<int>(i));
                ImGui::InvisibleButton("word",ImVec2(std::max(1.0f,size.x),line_height));
                hover=ImGui::IsItemHovered();
                if(ImGui::IsItemClicked()) {
                    if(ImGui::GetIO().KeyShift && selection_anchor_>=0) {
                        selection_end_=static_cast<int>(i);
                        const auto first=spans[std::min(selection_anchor_,selection_end_)];
                        const auto last=spans[std::max(selection_anchor_,selection_end_)];
                        selected_text_=text.substr(first.start,last.start+last.length-first.start);
                        lookup(selected_text_,true);
                    } else {selection_anchor_=selection_end_=static_cast<int>(i);lookup(token,true);}
                }
                if(hover) ImGui::SetMouseCursor(ImGuiMouseCursor_Hand);
                if (static_cast<int>(i)==selection_end_) gloss_on_line=true;
                ImGui::PopID();
            }
            const bool selected=selection_anchor_>=0 && static_cast<int>(i)>=std::min(selection_anchor_,selection_end_) && static_cast<int>(i)<=std::max(selection_anchor_,selection_end_);
            if(selected) draw->AddRectFilled(at,ImVec2(at.x+size.x,at.y+line_height),ImGui::GetColorU32(blend(theme_.accent,theme_.accent,0,0.27f)),3.0f);
            draw->AddText(at,ImGui::GetColorU32(hover?theme_.accent:theme_.text),token.c_str());
            if(hover || selected) draw->AddLine(ImVec2(at.x,at.y+size.y+1),ImVec2(at.x+size.x,at.y+size.y+1),ImGui::GetColorU32(theme_.accent),selected ? 2.f : 1.f);
            x+=size.x;
        }
        advance_line();
        ImGui::SetCursorScreenPos(ImVec2(origin.x,origin.y+y));
        ImGui::Dummy(ImVec2(1,1));
    }
};

std::unique_ptr<Panel> g_panel;
std::shared_ptr<bilingual::RuntimeStatus> g_health;
template<class F> void update_health(F&& update) noexcept {
    try {if(g_health) update(*g_health);} catch(...) {
        OutputDebugStringW(L"BilingualPanel: health snapshot update failed.\n");
    }
}
void on_overlay(reshade::api::effect_runtime* runtime) {
    if (!g_panel) return;
    try { g_panel->render(runtime); }
    catch (const std::exception& error) { g_panel->report_exception(error.what(),runtime); }
    catch (...) { g_panel->report_exception("unknown C++ exception",runtime); }
}
void on_present(reshade::api::command_queue*, reshade::api::swapchain* swapchain,
                const reshade::api::rect*, const reshade::api::rect*, uint32_t, const reshade::api::rect*) {
    if (!g_panel) return;
    try { g_panel->before_present(swapchain); }
    catch (const std::exception& error) { g_panel->report_exception(error.what()); }
    catch (...) { g_panel->report_exception("input preparation exception"); }
}
void on_runtime_destroyed(reshade::api::effect_runtime* runtime) {
    if (g_panel) g_panel->runtime_destroyed(runtime);
    update_health([&](auto& status){status.runtime_destroyed(runtime);});
}
void on_runtime_initialized(reshade::api::effect_runtime* runtime) {
    update_health([&](auto& status){status.runtime_initialized(runtime);});
}
}

extern "C" __declspec(dllexport) const char* NAME = "Bilingual Game Workbench";
extern "C" __declspec(dllexport) const char* DESCRIPTION = "Local bilingual reading and vocabulary, rendered inside the game. F8 to open.";

extern "C" __declspec(dllexport) bool AddonInit(HMODULE addon_module,HMODULE) {
    // AddonInit is ReShade's explicit initialization entry point, not DllMain.
    // The writer does disk I/O off-thread; telemetry failure must not alter play.
    try {g_health=std::make_shared<bilingual::RuntimeStatus>();}
    catch(...) {OutputDebugStringW(L"BilingualPanel: cannot initialize startup health reporting.\n");}
    if (!reshade::register_addon(addon_module)) {
        try {const auto config=read_config(addon_module);update_health([&](auto& status){status.game(config.game_id);});} catch(...) {}
        update_health([](auto& status){status.error("register_failed","ReShade rejected add-on registration (runtime/API or ImGui version mismatch).");});
        g_health.reset();
        return false;
    }
    update_health([](auto& status){status.loaded();});
    const char* failure_code="config_error";
    try {
        auto config=read_config(addon_module);
        update_health([&](auto& status){status.game(config.game_id);});
        log_event(config,"Add-on initialized; F8 opens in-game panel");
        failure_code="init_error";
        g_panel=std::make_unique<Panel>(std::move(config),g_health);
        reshade::register_event<reshade::addon_event::present>(&on_present);
        reshade::register_event<reshade::addon_event::init_effect_runtime>(&on_runtime_initialized);
        reshade::register_event<reshade::addon_event::destroy_effect_runtime>(&on_runtime_destroyed);
        reshade::register_event<reshade::addon_event::reshade_overlay>(&on_overlay);
        return true;
    } catch (const std::exception& error) {
        update_health([&](auto& status){status.error(failure_code,error.what());});
        try {Config config;config.directory=module_directory(addon_module);log_event(config,(std::string("Add-on initialization failed: ")+error.what()).c_str());} catch(...) {}
    } catch (...) {
        update_health([&](auto& status){status.error(failure_code,"Unknown add-on initialization exception.");});
        try {Config config;config.directory=module_directory(addon_module);log_event(config,"Add-on initialization failed: unknown exception");} catch(...) {}
    }
    g_panel.reset();
    update_health([](auto& status){status.unloaded();});
    g_health.reset();
    reshade::unregister_addon(addon_module);
    return false;
}
extern "C" __declspec(dllexport) void AddonUninit(HMODULE addon_module,HMODULE) {
    reshade::unregister_event<reshade::addon_event::reshade_overlay>(&on_overlay);
    reshade::unregister_event<reshade::addon_event::present>(&on_present);
    reshade::unregister_event<reshade::addon_event::init_effect_runtime>(&on_runtime_initialized);
    reshade::unregister_event<reshade::addon_event::destroy_effect_runtime>(&on_runtime_destroyed);
    g_panel.reset();
    update_health([](auto& status){status.unloaded();});
    g_health.reset();
    reshade::unregister_addon(addon_module);
}
