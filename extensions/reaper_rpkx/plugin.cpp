// Experimental REAPER extension. Retain native decoding and read-only getters;
// never import or invoke a native peak writer. See README for coverage limits.
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <tuple>
#include <vector>
#include "reaper_plugin.h"
#include "bridge.h"
#ifdef min
#undef min
#endif
#ifdef max
#undef max
#endif
namespace fs=std::filesystem;
using Clock=std::chrono::steady_clock;
static PCM_source* (*create_file)(const char*);
static PCM_source* (*create_type)(const char*);
static void (*peak_name)(const char*,char*,int,bool);
static REAPER_PeakGet_Interface* (*peak_get)(const char*,int,int);
static void (*hires)(PCM_source*,PCM_source_peaktransfer_t*);
static void* (*config_var)(const char*,int*);
static void (*console)(const char*);
static void (*update_arrange)();
static int (*regfn)(const char*,void*);
static const char* (*app_version)();
static thread_local unsigned delegate_depth=0;
static std::mutex log_mu;
static std::string log_path;
static std::atomic<unsigned> serial{0};
static constexpr size_t PCM_BUDGET=256*1024*1024;
static double elapsed(Clock::time_point a){return std::chrono::duration<double>(Clock::now()-a).count();}
static std::string error_text(){char b[2048]{};lrpk_last_error(b,sizeof b);return b;}
static void log(const std::string&s){std::lock_guard<std::mutex>g(log_mu);if(!log_path.empty()){std::ofstream f(fs::u8path(log_path),std::ios::app);f<<s<<'\n';}}
static uint32_t u32(const uint8_t*p){return uint32_t(p[0])|uint32_t(p[1])<<8|uint32_t(p[2])<<16|uint32_t(p[3])<<24;}
static int cfg(const char*n,int fallback){int size=0;auto*p=config_var?static_cast<int*>(config_var(n,&size)):nullptr;return p&&size==sizeof(int)?*p:fallback;}
static int requested_mode(){const int s=cfg("showpeaks",1);return (s&256)?2:((s&(32|8192|16384|32768|262144))?1:0);}
struct Delegating{Delegating(){++delegate_depth;}~Delegating(){--delegate_depth;}};
struct Buffer{
    LrpkBuffer b{};
    Buffer()=default;~Buffer(){lrpk_free(&b);}
    Buffer(const Buffer&)=delete;Buffer&operator=(const Buffer&)=delete;
    Buffer(Buffer&&o)noexcept:b(o.b){o.b={};}
    Buffer&operator=(Buffer&&o)noexcept{if(this!=&o){lrpk_free(&b);b=o.b;o.b={};}return*this;}
};
struct Stamp{uintmax_t size=0;fs::file_time_type time{};bool operator==(const Stamp&o)const{return size==o.size&&time==o.time;}};
static Stamp stat_file(const std::string&p){auto f=fs::u8path(p);return {fs::file_size(f),fs::last_write_time(f)};}
static bool supported(const char*t){return t&&(!strcmp(t,"WAVE")||!strcmp(t,"FLAC")||!strcmp(t,"MP3")||!strcmp(t,"VORBIS")||!strcmp(t,"WAVPACK"));}
struct Pair{double hi=-1e300,lo=1e300;};
struct Result{Buffer image;std::string error;bool reuse=false;LrpkReport report{};double generation_s=0,commit_s=0;};
#include "windows_guard.h"
#include "plugin_job.h"
static std::mutex jobs_mu;
using JobKey=std::tuple<std::string,uintmax_t,fs::file_time_type,int,int>;
static std::map<JobKey,std::weak_ptr<Job>> jobs;
class Source;
static std::set<Source*> sources;
static std::mutex sources_mu;
class Source final:public PCM_source{
    std::unique_ptr<PCM_source> inner;
    std::unique_ptr<REAPER_PeakGet_Interface> getter;
    Stamp getter_stamp;
    std::string cache,error,clear_error,rebuild_cache;
    std::shared_ptr<Job> job;
    bool dirty=false,online=true,online_recheck=false,timer_owned=false;
    std::atomic<bool> displayed{false};
    std::atomic<int> peak_mode_hint{0};
    int observed_mode=0,observed_pps=300;
    int desired_mode()const{return std::max(requested_mode(),peak_mode_hint.load());}
public:
    explicit Source(PCM_source*p):inner(p),observed_mode(requested_mode()),observed_pps(cfg("peakcachegenrs",300)){
        std::lock_guard<std::mutex>g(sources_mu);sources.insert(this);
    }
    ~Source()override{std::lock_guard<std::mutex>g(sources_mu);sources.erase(this);}
    PCM_source*Duplicate()override{try{Delegating g;auto*p=inner->Duplicate();return p?new Source(p):nullptr;}catch(...){return nullptr;}}
    bool IsAvailable()override{return inner->IsAvailable();}
    void SetAvailable(bool v)override{
        getter.reset();cache.clear();error.clear();clear_error.clear();online=v;
        if(!v)timer_owned=false;
        inner->SetAvailable(v);
        // Reopening can alter the source stamp even if REAPER doesn't invoke
        // PeaksBuild_Begin. Queue validation; service() skips disk IO when the
        // just-committed job already proves the same source/profile is ready.
        online_recheck=v;
        log(std::string("AVAILABLE\tvalue=")+(v?"1":"0")+"\tfile="+(GetFileName()?GetFileName():""));
    }
    const char*GetType()override{return inner->GetType();}
    const char*GetFileName()override{return inner->GetFileName();}
    bool SetFileName(const char*s)override{getter.reset();job.reset();cache.clear();clear_error.clear();rebuild_cache.clear();return inner->SetFileName(s);}
    PCM_source*GetSource()override{return inner->GetSource();}
    void SetSource(PCM_source*p)override{inner->SetSource(p);}
    int GetNumChannels()override{return inner->GetNumChannels();}
    double GetSampleRate()override{return inner->GetSampleRate();}
    double GetLength()override{return inner->GetLength();}
    double GetLengthBeats()override{return inner->GetLengthBeats();}
    int GetBitsPerSample()override{return inner->GetBitsPerSample();}
    double GetPreferredPosition()override{return inner->GetPreferredPosition();}
    int PropertiesWindow(HWND h)override{return inner->PropertiesWindow(h);}
    void GetSamples(PCM_source_transfer_t*b)override{inner->GetSamples(b);}
    void SaveState(ProjectStateContext*c)override{inner->SaveState(c);}
    int LoadState(const char*s,ProjectStateContext*c)override{getter.reset();cache.clear();job.reset();clear_error.clear();rebuild_cache.clear();return inner->LoadState(s,c);}
    int Extended(int c,void*a,void*b,void*d)override{return inner->Extended(c,a,b,d);}
    void Peaks_Clear(bool remove)override{
        getter.reset();job.reset();error.clear();clear_error.clear();timer_owned=false;rebuild_cache.clear();
        log("CLEAR\tdelete_requested="+std::to_string(remove)+"\tfile="+(GetFileName()?GetFileName():""));
        try{
            if(remove){
#ifdef _WIN32
                rebuild_cache=lrpk_prepare_guarded_clear(inner.get(),GetFileName());
#else
                Delegating g;inner->Peaks_Clear(false);
#endif
                dirty=true;
            }else{Delegating g;inner->Peaks_Clear(false);}
        }catch(const std::exception&e){dirty=false;clear_error=e.what();error=clear_error;log("ERROR\tclear\t"+clear_error);if(console)console(("libreapeaks: "+clear_error+"\n").c_str());}
    }
    int PeaksBuild_Begin()override{
        try{
            if(!clear_error.empty())throw std::runtime_error(clear_error);
            if(job&&job->state<3)return 1;
            observed_mode=desired_mode();observed_pps=cfg("peakcachegenrs",300);online_recheck=false;
            getter.reset();error.clear();
            const std::string media=GetFileName()?GetFileName():"";
            const auto st=stat_file(media);
            const JobKey key{media,st.size,st.time,observed_mode,observed_pps};
            std::lock_guard<std::mutex>g(jobs_mu);
            for(auto it=jobs.begin();it!=jobs.end();){if(it->second.expired())it=jobs.erase(it);else ++it;}
            auto existing=jobs[key].lock();
            if(existing&&existing->state<3&&(!dirty||existing->force))job=existing;
            else{job=std::make_shared<Job>(inner.get(),dirty,observed_mode,dirty?rebuild_cache:std::string{});jobs[key]=job;}
            dirty=false;rebuild_cache.clear();cache=job->cache;return 1;
        }catch(const std::exception&e){error=e.what();log("ERROR\tbegin\t"+error);if(console)console(("libreapeaks: "+error+"\n").c_str());return 0;}
    }
    int PeaksBuild_Run()override{return job?job->run():0;}
    void PeaksBuild_Finish()override{
        if(job&&job->state==4){error=job->error;if(!job->reported&&console){console(("libreapeaks: "+error+"\n").c_str());job->reported=true;}}
        if(job&&job->state<2){job.reset();dirty=true;}
        getter.reset();timer_owned=false;
    }
    // Called only by REAPER's main-thread timer. No command interception or
    // test API is needed for a display-profile change or online transition.
    bool service(std::set<unsigned>&advanced){
        if(!online)return false;
        if(!timer_owned&&(!job||job->state>=3)&&(displayed.load()||online_recheck)){
            const int mode=desired_mode(),pps=cfg("peakcachegenrs",300);
            if(online_recheck&&job&&job->state==3){
                try{
                    const char*fn=GetFileName();
                    if(fn&&*fn&&stat_file(fn)==job->source_stamp&&mode<=int(job->mode)&&pps==int(job->pps)){
                        observed_mode=mode;observed_pps=pps;online_recheck=false;
                        log("RECHECK_SKIP\treason=online-ready\tmode="+std::to_string(mode)+"\tfile="+fn);
                    }
                }catch(...){/* A real stamp/read problem falls through to validation. */}
            }
            if(online_recheck||mode!=observed_mode||pps!=observed_pps){
                const bool reopening=online_recheck;
                observed_mode=mode;observed_pps=pps;online_recheck=false;
                log("RECHECK\treason="+std::string(reopening?"online":"profile")+"\tmode="+std::to_string(mode)+"\tshowpeaks="+std::to_string(cfg("showpeaks",1))+"\tfile="+(GetFileName()?GetFileName():""));
                timer_owned=PeaksBuild_Begin()!=0;
            }
        }
        if(!timer_owned||!job)return false;
        if(job->state>=3){PeaksBuild_Finish();return true;}
        if(!advanced.insert(job->id).second)return false;
        if(PeaksBuild_Run()==0)PeaksBuild_Finish();
        return true;
    }
    void GetPeakInfo(PCM_source_peaktransfer_t*b)override{
        b->peaks_out=0;b->peaks_minvals_used=0;b->extra_requested_data_out=0;b->extra_requested_data_out2=0;
        if(!b->peaks||b->numpeak_points<1||b->numpeak_points>1000000||!std::isfinite(b->start_time)||!std::isfinite(b->peakrate))return;
        displayed=true;
        // Some host views request extra peak data without changing showpeaks.
        // Record the requirement only; the timer performs the work.
        for(int type:{b->extra_requested_data_type,b->extra_requested_data_type2}){
            const int need=(type=='g'||type=='G')?2:((type=='s'||type=='r')?1:0);
            int seen=peak_mode_hint.load();while(seen<need&&!peak_mode_hint.compare_exchange_weak(seen,need)){}
        }
        try{
            if(job&&job->state<3){job->live_peaks(b);return;}
            if(cache.empty()){const char*fn=GetFileName();if(!fn||!*fn)return;char p[32768]{};peak_name(fn,p,sizeof p,false);cache=p;}
            void*guard=lrpk_try_read_guard(cache.c_str());if(!guard)return;
            struct Guard{void*p;~Guard(){lrpk_release_read_guard(p);}}g{guard};
            const auto stamp=stat_file(cache);
            if(!getter||!(stamp==getter_stamp)){getter.reset();getter.reset(peak_get(GetFileName(),int(GetSampleRate()),GetNumChannels()));getter_stamp=stamp;}
            if(getter){
                const double maxres=REAPER_PEAKRES_MAX_FOR_BLOCK(b,getter->GetMaxPeakRes());
                if(hires&&b->peakrate>=maxres){b->__peakgetter=getter.get();hires(inner.get(),b);b->__peakgetter=nullptr;if(b->peaks_out)return;}
                getter->GetPeakInfo(b);
            }
        }catch(const std::exception&e){error=e.what();}
    }
    int status()const{return !error.empty()||(job&&job->state==4)?-1:(job?(job->state==3?2:1):0);}
};
static bool timer_running=false;
static void service_sources(){
    if(timer_running)return;
    struct Running{Running(){timer_running=true;}~Running(){timer_running=false;}}running;
    bool changed=false;
    try{
        std::unique_lock<std::mutex>g(sources_mu,std::try_to_lock);if(!g.owns_lock())return;
        std::set<unsigned>advanced;const auto started=Clock::now();
        for(auto*s:sources){changed=s->service(advanced)||changed;if(elapsed(started)>0.008)break;}
        g.unlock();
        if(changed&&update_arrange)update_arrange();
    }catch(const std::exception&e){log(std::string("ERROR\ttimer\t")+e.what());}catch(...){log("ERROR\ttimer\tunknown exception");}
}
static PCM_source*from_file(const char*p,int priority){
    if(!p||priority||delegate_depth)return nullptr;
    try{Delegating g;auto*src=create_file(p);if(!src)return nullptr;if(!supported(src->GetType())){log(std::string("UNWRAPPED\ttype=")+src->GetType()+"\tfile="+p);return src;}return new Source(src);}catch(...){return nullptr;}
}
static PCM_source*from_type(const char*p,int priority){
    if(!supported(p)||priority||delegate_depth)return nullptr;
    try{Delegating g;auto*src=create_type(p);return src?new Source(src):nullptr;}catch(...){return nullptr;}
}
static const char*extensions(int,const char**desc){if(desc)*desc=nullptr;return nullptr;}
static pcmsrc_register_t provider={from_type,from_file,extensions};
static Source*checked(PCM_source*p){std::lock_guard<std::mutex>g(sources_mu);auto*s=static_cast<Source*>(p);return sources.count(s)?s:nullptr;}
static int force_build(PCM_source*p){if(auto*s=checked(p)){s->Peaks_Clear(true);return 1;}return 0;}
static int status(PCM_source*p){if(auto*s=checked(p))return s->status();return -2;}
static void*force_va(void**a,int n){return reinterpret_cast<void*>(static_cast<intptr_t>(n==1?force_build(static_cast<PCM_source*>(a[0])):0));}
static void*status_va(void**a,int n){return reinterpret_cast<void*>(static_cast<intptr_t>(n==1?status(static_cast<PCM_source*>(a[0])):-2));}
extern "C" REAPER_PLUGIN_DLL_EXPORT int REAPER_PLUGIN_ENTRYPOINT(REAPER_PLUGIN_HINSTANCE,reaper_plugin_info_t*r){
    if(!r){if(regfn){regfn("-timer",reinterpret_cast<void*>(service_sources));regfn("-pcmsrc",&provider);}return 0;}
    if(r->caller_version!=REAPER_PLUGIN_VERSION||!r->GetFunc||!r->Register)return 0;
    regfn=r->Register;
#define LOAD(name,var) var=reinterpret_cast<decltype(var)>(r->GetFunc(name));if(!var)return 0
    LOAD("PCM_Source_CreateFromFile",create_file);LOAD("PCM_Source_CreateFromType",create_type);
    LOAD("GetPeakFileNameEx",peak_name);LOAD("PeakGet_Create",peak_get);LOAD("get_config_var",config_var);
    LOAD("ShowConsoleMsg",console);LOAD("GetAppVersion",app_version);LOAD("UpdateArrange",update_arrange);
#undef LOAD
    hires=reinterpret_cast<decltype(hires)>(r->GetFunc("HiresPeaksFromSource"));
    if(const char*p=std::getenv("LIBREAPEAKS_PLUGIN_LOG"))log_path=p;
    log(std::string("LOAD\tversion=")+app_version());
#ifdef LRPK_TEST_HOOKS
    log("DIAGNOSTIC_BUILD\tfault_hooks=1");
#endif
    if(std::strncmp(app_version(),"7.79",4)){console("libreapeaks experimental plugin is pinned to REAPER 7.79; it was not loaded.\n");return 0;}
    if(!r->Register("<pcmsrc",&provider))return 0;
    if(!r->Register("timer",reinterpret_cast<void*>(service_sources))){r->Register("-pcmsrc",&provider);return 0;}
    r->Register("ext_name",const_cast<char*>("libreapeaks RPKX protection (experimental)"));
    r->Register("ext_vendor",const_cast<char*>("libreapeaks"));
    r->Register("API_RPKX_ForceBuild",reinterpret_cast<void*>(force_build));
    r->Register("APIdef_RPKX_ForceBuild",const_cast<char*>("int\0PCM_source*\0source\0Mark a wrapped source for a preserving rebuild."));
    r->Register("APIvararg_RPKX_ForceBuild",reinterpret_cast<void*>(force_va));
    r->Register("API_RPKX_Status",reinterpret_cast<void*>(status));
    r->Register("APIdef_RPKX_Status",const_cast<char*>("int\0PCM_source*\0source\0-2 not wrapped; -1 failed; 0 idle; 1 busy; 2 ready."));
    r->Register("APIvararg_RPKX_Status",reinterpret_cast<void*>(status_va));
    return 1;
}