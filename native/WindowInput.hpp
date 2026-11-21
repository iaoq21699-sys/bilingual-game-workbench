#pragma once

// Queued input for composition swap chains and games that deliver input directly to their window.
// The window procedure only queues messages. Only the presenting thread writes
// the existing ReShade ImGuiIO, before its next NewFrame (ImGui 1.92.5 ABI).
#include <windows.h>
#include <windowsx.h>
#include <imm.h>
#include <imgui.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <cfloat>
#include <deque>
#include <memory>
#include <mutex>
#include <unordered_map>

namespace bilingual {
class WindowInput {
    enum class Kind { Key, Character, Mouse, Move, Wheel, Reset };
    struct Event { Kind kind; unsigned code=0; bool down=false; float x=0,y=0; };
    struct State {
        HWND window=nullptr;
        WNDPROC previous=nullptr;
        std::atomic<int> mode{0}; // Hidden, Interactive, Pinned
        std::atomic<unsigned> toggles{0};
        std::atomic<bool> alive{true};
        std::mutex mutex;
        std::deque<Event> events;
        std::array<bool,256> consumed_keys{};
        std::array<bool,5> consumed_mouse{};
        std::array<bool,256> forwarded_keys{};
        std::array<bool,5> forwarded_mouse{};
        HIMC previous_ime=nullptr,owned_ime=nullptr;
        bool ime_active=false;
    };
    inline static std::mutex registry_mutex_;
    inline static std::unordered_map<HWND,std::shared_ptr<State>> registry_;
    inline static const UINT sync_message_=RegisterWindowMessageW(L"BilingualWorkbench.InputSync.0.5.2");
    std::shared_ptr<State> state_;
    ULONGLONG next_attempt_=0;
    bool was_focused_=false;
    unsigned short surrogate_=0;

    static bool focused(HWND hwnd) {
        HWND foreground=GetForegroundWindow();
        DWORD process=0;
        GetWindowThreadProcessId(foreground,&process);
        return process==GetCurrentProcessId() && foreground &&
            (foreground==hwnd || IsChild(hwnd,foreground));
    }
    static bool eligible(HWND hwnd) {
        DWORD process=0;
        GetWindowThreadProcessId(hwnd,&process);
        RECT area{};
        return process==GetCurrentProcessId() && IsWindowVisible(hwnd) &&
            !(GetWindowLongPtrW(hwnd,GWL_STYLE)&WS_CHILD) &&
            !(GetWindowLongPtrW(hwnd,GWL_EXSTYLE)&WS_EX_TOOLWINDOW) &&
            GetWindow(hwnd,GW_OWNER)==nullptr && GetClientRect(hwnd,&area) &&
            area.right>=100 && area.bottom>=100;
    }
    static void enqueue(State& s,Event event) {
        std::lock_guard lock(s.mutex);
        if(s.events.size()>=4096) {
            s.events.clear();
            s.events.push_back({Kind::Reset});
        }
        if(event.kind==Kind::Move && !s.events.empty() && s.events.back().kind==Kind::Move)
            s.events.back()=event;
        else s.events.push_back(event);
    }
    static void sync_ime(State& s) {
        const bool active=s.mode.load()==1 && focused(s.window) && s.alive.load();
        if(active==s.ime_active) return;
        if(active) {
            s.previous_ime=ImmGetContext(s.window);
            if(s.previous_ime) ImmReleaseContext(s.window,s.previous_ime);
            else {
                s.owned_ime=ImmCreateContext();
                if(s.owned_ime) ImmAssociateContext(s.window,s.owned_ime);
            }
        } else if(s.owned_ime) {
            ImmAssociateContext(s.window,s.previous_ime);
            ImmDestroyContext(s.owned_ime);
            s.owned_ime=nullptr;
        }
        s.ime_active=active;
    }
    static LRESULT CALLBACK procedure(HWND hwnd,UINT message,WPARAM wp,LPARAM lp) noexcept {
        std::shared_ptr<State> s;
        {
            std::lock_guard lock(registry_mutex_);
            auto it=registry_.find(hwnd);
            if(it!=registry_.end()) s=it->second;
        }
        if(!s || !s->previous) return DefWindowProcW(hwnd,message,wp,lp);
        try {
            if(message==WM_NCDESTROY) {
                s->alive=false;
                s->mode=0;
                sync_ime(*s);
                if(reinterpret_cast<WNDPROC>(GetWindowLongPtrW(hwnd,GWLP_WNDPROC))==procedure)
                    SetWindowLongPtrW(hwnd,GWLP_WNDPROC,reinterpret_cast<LONG_PTR>(s->previous));
                const auto result=CallWindowProcW(s->previous,hwnd,message,wp,lp);
                std::lock_guard lock(registry_mutex_);
                registry_.erase(hwnd);
                return result;
            }
            if(message==sync_message_) {sync_ime(*s);return 0;}
            if(!s->alive.load()) return CallWindowProcW(s->previous,hwnd,message,wp,lp);
            if(message==WM_KILLFOCUS || (message==WM_ACTIVATEAPP && !wp)) {
                enqueue(*s,{Kind::Reset});
                sync_ime(*s);
            }
            const bool active=focused(hwnd);
            const bool key=message==WM_KEYDOWN || message==WM_SYSKEYDOWN || message==WM_KEYUP || message==WM_SYSKEYUP;
            if(key && active && wp<256) {
                const bool down=message==WM_KEYDOWN || message==WM_SYSKEYDOWN;
                const bool repeat=(lp&(static_cast<LPARAM>(1)<<30))!=0;
                if(wp==VK_F8) {
                    if(down && !repeat) {
                        s->mode=s->mode.load()==1?0:1;
                        ++s->toggles;
                        enqueue(*s,{Kind::Reset});
                        sync_ime(*s);
                    }
                    return 0; // Also consume its repeat and release after closing.
                }
                if(wp==VK_ESCAPE && s->mode.load()==1) {
                    if(down && !repeat) {s->mode=0;enqueue(*s,{Kind::Reset});sync_ime(*s);}
                    s->consumed_keys[VK_ESCAPE]=down;
                    return 0;
                }
                if(s->mode.load()==1) {
                    unsigned code=static_cast<unsigned>(wp);
                    if(code==VK_SHIFT) code=MapVirtualKeyW((static_cast<unsigned>(lp)>>16)&255,MAPVK_VSC_TO_VK_EX);
                    if(code==VK_CONTROL) code=(lp&(static_cast<LPARAM>(1)<<24))?VK_RCONTROL:VK_LCONTROL;
                    if(code==VK_MENU) code=(lp&(static_cast<LPARAM>(1)<<24))?VK_RMENU:VK_LMENU;
                    enqueue(*s,{Kind::Key,code,down});
                    s->consumed_keys[static_cast<unsigned>(wp)]=down;
                    // A key held before opening must still release in the game.
                    if(!down && s->forwarded_keys[static_cast<unsigned>(wp)]) {
                        s->forwarded_keys[static_cast<unsigned>(wp)]=false;
                        CallWindowProcW(s->previous,hwnd,message,wp,lp);
                    }
                    return 0;
                }
                if(!down && s->consumed_keys[static_cast<unsigned>(wp)]) {
                    s->consumed_keys[static_cast<unsigned>(wp)]=false;
                    return 0;
                }
                s->forwarded_keys[static_cast<unsigned>(wp)]=down;
            }
            const bool capture=active && s->mode.load()==1;
            if(capture) {
                if(message==WM_CHAR || message==WM_IME_CHAR || message==WM_UNICHAR) {
                    if(message==WM_UNICHAR && wp==UNICODE_NOCHAR) return TRUE;
                    if(wp>=32 && wp!=127) enqueue(*s,{Kind::Character,static_cast<unsigned>(wp)});
                    return 0;
                }
                if(message==WM_IME_STARTCOMPOSITION || message==WM_IME_ENDCOMPOSITION || message==WM_IME_COMPOSITION)
                    return DefWindowProcW(hwnd,message,wp,lp);
                if(message==WM_SYSCHAR || message==WM_DEADCHAR || message==WM_SYSDEADCHAR) return 0;
                if(message==WM_INPUT) return DefWindowProcW(hwnd,message,wp,lp); // Raw-input cleanup, no game dispatch.
                if(message==WM_SETCURSOR && LOWORD(lp)==HTCLIENT) {SetCursor(LoadCursorW(nullptr,IDC_ARROW));return TRUE;}
                if(message==WM_MOUSEMOVE) {
                    enqueue(*s,{Kind::Move,0,false,static_cast<float>(GET_X_LPARAM(lp)),static_cast<float>(GET_Y_LPARAM(lp))});
                    return 0;
                }
                if(message==WM_MOUSEWHEEL || message==WM_MOUSEHWHEEL) {
                    const float delta=static_cast<float>(GET_WHEEL_DELTA_WPARAM(wp))/WHEEL_DELTA;
                    enqueue(*s,{Kind::Wheel,0,false,message==WM_MOUSEHWHEEL?-delta:0,message==WM_MOUSEWHEEL?delta:0});
                    return 0;
                }
            }
            int button=-1;bool down=false;
            switch(message) {
            case WM_LBUTTONDOWN:case WM_LBUTTONDBLCLK:button=0;down=true;break;
            case WM_LBUTTONUP:button=0;break;
            case WM_RBUTTONDOWN:case WM_RBUTTONDBLCLK:button=1;down=true;break;
            case WM_RBUTTONUP:button=1;break;
            case WM_MBUTTONDOWN:case WM_MBUTTONDBLCLK:button=2;down=true;break;
            case WM_MBUTTONUP:button=2;break;
            case WM_XBUTTONDOWN:case WM_XBUTTONDBLCLK:button=GET_XBUTTON_WPARAM(wp)==XBUTTON1?3:4;down=true;break;
            case WM_XBUTTONUP:button=GET_XBUTTON_WPARAM(wp)==XBUTTON1?3:4;break;
            }
            if(button>=0 && active) {
                if(capture) {
                    enqueue(*s,{Kind::Move,0,false,static_cast<float>(GET_X_LPARAM(lp)),static_cast<float>(GET_Y_LPARAM(lp))});
                    enqueue(*s,{Kind::Mouse,static_cast<unsigned>(button),down});
                    s->consumed_mouse[button]=down;
                    if(!down && s->forwarded_mouse[button]) {
                        s->forwarded_mouse[button]=false;
                        CallWindowProcW(s->previous,hwnd,message,wp,lp);
                    }
                    return button>=3?TRUE:0;
                }
                if(!down && s->consumed_mouse[button]) {s->consumed_mouse[button]=false;return button>=3?TRUE:0;}
                s->forwarded_mouse[button]=down;
            }
        } catch(...) {s->mode=0;}
        return CallWindowProcW(s->previous,hwnd,message,wp,lp);
    }
    static ImGuiKey key(unsigned vk) {
        if(vk>='A'&&vk<='Z') return static_cast<ImGuiKey>(ImGuiKey_A+vk-'A');
        if(vk>='0'&&vk<='9') return static_cast<ImGuiKey>(ImGuiKey_0+vk-'0');
        if(vk>=VK_F1&&vk<=VK_F24) return static_cast<ImGuiKey>(ImGuiKey_F1+vk-VK_F1);
        switch(vk) {
        case VK_TAB:return ImGuiKey_Tab;case VK_LEFT:return ImGuiKey_LeftArrow;case VK_RIGHT:return ImGuiKey_RightArrow;
        case VK_UP:return ImGuiKey_UpArrow;case VK_DOWN:return ImGuiKey_DownArrow;case VK_PRIOR:return ImGuiKey_PageUp;
        case VK_NEXT:return ImGuiKey_PageDown;case VK_HOME:return ImGuiKey_Home;case VK_END:return ImGuiKey_End;
        case VK_INSERT:return ImGuiKey_Insert;case VK_DELETE:return ImGuiKey_Delete;case VK_BACK:return ImGuiKey_Backspace;
        case VK_SPACE:return ImGuiKey_Space;case VK_RETURN:return ImGuiKey_Enter;case VK_ESCAPE:return ImGuiKey_Escape;
        case VK_LCONTROL:return ImGuiKey_LeftCtrl;case VK_RCONTROL:return ImGuiKey_RightCtrl;
        case VK_LSHIFT:return ImGuiKey_LeftShift;case VK_RSHIFT:return ImGuiKey_RightShift;
        case VK_LMENU:return ImGuiKey_LeftAlt;case VK_RMENU:return ImGuiKey_RightAlt;
        case VK_LWIN:return ImGuiKey_LeftSuper;case VK_RWIN:return ImGuiKey_RightSuper;
        case VK_OEM_1:return ImGuiKey_Semicolon;case VK_OEM_PLUS:return ImGuiKey_Equal;
        case VK_OEM_COMMA:return ImGuiKey_Comma;case VK_OEM_MINUS:return ImGuiKey_Minus;case VK_OEM_PERIOD:return ImGuiKey_Period;
        case VK_OEM_2:return ImGuiKey_Slash;case VK_OEM_3:return ImGuiKey_GraveAccent;
        case VK_OEM_4:return ImGuiKey_LeftBracket;case VK_OEM_5:return ImGuiKey_Backslash;
        case VK_OEM_6:return ImGuiKey_RightBracket;case VK_OEM_7:return ImGuiKey_Apostrophe;
        default:return ImGuiKey_None;
        }
    }
    static void set_key(ImGuiIO& io,ImGuiKey code,bool down) {
        if(code==ImGuiKey_None) return;
        auto& value=io.KeysData[code-ImGuiKey_NamedKey_BEGIN];
        value.Down=down;value.AnalogValue=down?1.0f:0.0f;
    }
    static void clear(ImGuiIO& io) {
        for(auto& value:io.KeysData) {value.Down=false;value.AnalogValue=0;}
        for(bool& value:io.MouseDown) value=false;
        io.MouseWheel=io.MouseWheelH=0;
        io.InputQueueCharacters.clear();
    }
public:
    ~WindowInput(){disconnect();}
    WindowInput()=default;
    WindowInput(const WindowInput&)=delete;
    WindowInput& operator=(const WindowInput&)=delete;
    // 1 = newly bound, 0 = existing/retry delay, -1 = no eligible window or failure.
    int ensure(HWND render_window=nullptr) {
        if(state_ && state_->alive.load() && IsWindow(state_->window)) return 0;
        if(GetTickCount64()<next_attempt_) return 0;
        next_attempt_=GetTickCount64()+1000;
        disconnect();
        HWND chosen=render_window ? GetAncestor(render_window,GA_ROOT) : GetForegroundWindow();
        if (render_window && !eligible(chosen)) return -1;
        if(!eligible(chosen)) {
            struct Choice {HWND hwnd=nullptr;LONG64 area=0;} choice;
            EnumWindows([](HWND hwnd,LPARAM data)->BOOL {
                auto& best=*reinterpret_cast<Choice*>(data);
                if(eligible(hwnd)) {RECT r{};GetClientRect(hwnd,&r);const LONG64 area=static_cast<LONG64>(r.right)*r.bottom;
                    if(area>best.area) {best.hwnd=hwnd;best.area=area;}}
                return TRUE;
            },reinterpret_cast<LPARAM>(&choice));
            chosen=choice.hwnd;
        }
        if(!chosen) return -1;
        auto s=std::make_shared<State>();s->window=chosen;
        std::lock_guard lock(registry_mutex_);
        if(registry_.contains(chosen)) return -1;
        registry_[chosen]=s;
        SetLastError(0);
        s->previous=reinterpret_cast<WNDPROC>(SetWindowLongPtrW(chosen,GWLP_WNDPROC,reinterpret_cast<LONG_PTR>(procedure)));
        if(!s->previous) {registry_.erase(chosen);return -1;}
        state_=std::move(s);
        return 1;
    }
    void disconnect() noexcept {
        auto s=std::move(state_);
        if(!s) return;
        s->mode=0;
        if(IsWindow(s->window)) {
            DWORD_PTR ignored=0;
            SendMessageTimeoutW(s->window,sync_message_,0,0,SMTO_ABORTIFHUNG|SMTO_BLOCK,100,&ignored);
        }
        s->alive=false;
        std::lock_guard lock(registry_mutex_);
        bool restored=!IsWindow(s->window);
        if(!restored && reinterpret_cast<WNDPROC>(GetWindowLongPtrW(s->window,GWLP_WNDPROC))==procedure) {
            SetLastError(0);
            restored=SetWindowLongPtrW(s->window,GWLP_WNDPROC,reinterpret_cast<LONG_PTR>(s->previous))!=0 || GetLastError()==0;
        }
        if(restored) {
            registry_.erase(s->window);
        } else {
            // A later subclass owns the head of the chain. Preserve its chain and
            // leave an inert forwarding node; pin our code until process exit.
            HMODULE module=nullptr;
            GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_PIN,
                reinterpret_cast<LPCWSTR>(procedure),&module);
        }
        was_focused_=false;
    }
    HWND window()const{return state_?state_->window:nullptr;}
    bool has_focus()const{return state_&&state_->alive.load()&&focused(state_->window);}
    int mode()const{return state_?state_->mode.load():0;}
    unsigned take_toggles(){return state_?state_->toggles.exchange(0):0;}
    void set_mode(int value) {
        if(!state_) return;
        const int previous=state_->mode.exchange(value);
        if(previous!=value) {enqueue(*state_,{Kind::Reset});PostMessageW(state_->window,sync_message_,0,0);}
    }
    void feed(ImGuiIO& io) {
        if(!state_) return;
        const bool focused_now=has_focus();
        if(!focused_now || mode()!=1) {
            clear(io);surrogate_=0;io.MousePos=ImVec2(-FLT_MAX,-FLT_MAX);
            std::lock_guard lock(state_->mutex);state_->events.clear();
            was_focused_=focused_now;return;
        }
        if(!was_focused_) clear(io);
        was_focused_=true;
        RECT client{};GetClientRect(state_->window,&client);
        const float sx=io.DisplaySize.x/std::max(1L,client.right),sy=io.DisplaySize.y/std::max(1L,client.bottom);
        POINT cursor{};
        if(GetCursorPos(&cursor)&&ScreenToClient(state_->window,&cursor)) io.MousePos=ImVec2(cursor.x*sx,cursor.y*sy);
        std::array<bool,256> touched_keys{};
        std::array<bool,5> touched_mouse{};
        std::lock_guard lock(state_->mutex);
        while(!state_->events.empty()) {
            const Event e=state_->events.front();
            // Keep a down/up pair across separate frames, even for fast synthetic
            // or physical clicks, so ImGui's NewFrame sees both edges.
            if(e.kind==Kind::Key&&e.code<256&&touched_keys[e.code]) break;
            if(e.kind==Kind::Mouse&&e.code<5&&touched_mouse[e.code]) break;
            state_->events.pop_front();
            if(e.kind==Kind::Reset) {clear(io);surrogate_=0;continue;}
            if(e.kind==Kind::Key&&e.code<256) {set_key(io,key(e.code),e.down);touched_keys[e.code]=true;}
            else if(e.kind==Kind::Mouse&&e.code<5) {io.MouseDown[e.code]=e.down;touched_mouse[e.code]=true;}
            else if(e.kind==Kind::Move) io.MousePos=ImVec2(e.x*sx,e.y*sy);
            else if(e.kind==Kind::Wheel) {io.MouseWheelH+=e.x;io.MouseWheel+=e.y;}
            else if(e.kind==Kind::Character) {
                unsigned code=e.code;
                if(code>=0xd800&&code<=0xdbff) {surrogate_=static_cast<unsigned short>(code);continue;}
                if(code>=0xdc00&&code<=0xdfff) code=surrogate_?0x10000+((surrogate_-0xd800)<<10)+(code-0xdc00):0xfffd;
                surrogate_=0;
                io.InputQueueCharacters.push_back(static_cast<ImWchar>(code>IM_UNICODE_CODEPOINT_MAX?0xfffd:code));
            }
        }
        const auto held=[&](ImGuiKey code){return io.KeysData[code-ImGuiKey_NamedKey_BEGIN].Down;};
        set_key(io,ImGuiKey_ReservedForModCtrl,held(ImGuiKey_LeftCtrl)||held(ImGuiKey_RightCtrl));
        set_key(io,ImGuiKey_ReservedForModShift,held(ImGuiKey_LeftShift)||held(ImGuiKey_RightShift));
        set_key(io,ImGuiKey_ReservedForModAlt,held(ImGuiKey_LeftAlt)||held(ImGuiKey_RightAlt));
        set_key(io,ImGuiKey_ReservedForModSuper,held(ImGuiKey_LeftSuper)||held(ImGuiKey_RightSuper));
    }
};
}
