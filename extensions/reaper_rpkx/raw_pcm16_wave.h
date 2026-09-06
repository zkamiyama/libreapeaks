#pragma once

// Strict fast path for ordinary little-endian RIFF/WAVE PCM16 files. Anything
// outside this narrow shape is not an error: the caller falls back to REAPER's
// native PCM_source decoder. This reader never owns more than one chunk of PCM.
struct RawPcm16Wave {
    std::ifstream file;
    std::vector<uint8_t> block;
    uint64_t data_offset=0,data_bytes=0,cursor=0;

    static uint16_t le16(const uint8_t*p){return uint16_t(p[0])|(uint16_t(p[1])<<8);}
    static uint32_t le32(const uint8_t*p){return uint32_t(p[0])|(uint32_t(p[1])<<8)|(uint32_t(p[2])<<16)|(uint32_t(p[3])<<24);}
    static bool add_ok(uint64_t a,uint64_t b,uint64_t&out){out=a+b;return out>=a;}

    void close(){
        if(file.is_open())file.close();
        file.clear();block.clear();data_offset=data_bytes=cursor=0;
    }

    bool open(const std::string&path,uint32_t rate,uint32_t nch,size_t frames){
        close();
        try{
            const auto p=fs::u8path(path);const uint64_t file_bytes=fs::file_size(p);
            if(file_bytes<44||file_bytes>uint64_t(UINT32_MAX)+8)return false;
            file.open(p,std::ios::binary);if(!file)return false;
            uint8_t head[12]{};file.read(reinterpret_cast<char*>(head),sizeof head);
            if(file.gcount()!=std::streamsize(sizeof head)||std::memcmp(head,"RIFF",4)||std::memcmp(head+8,"WAVE",4)||uint64_t(le32(head+4))+8!=file_bytes){close();return false;}
            const uint64_t frame_bytes=uint64_t(nch)*2;
            if(!frame_bytes||frames>UINT64_MAX/frame_bytes){close();return false;}
            const uint64_t expected_bytes=uint64_t(frames)*frame_bytes;
            bool have_fmt=false,have_data=false;uint64_t pos=12;
            for(unsigned chunks=0;chunks<1024&&pos+8<=file_bytes;++chunks){
                file.clear();file.seekg(std::streamoff(pos),std::ios::beg);if(!file){close();return false;}
                uint8_t ch[8]{};file.read(reinterpret_cast<char*>(ch),sizeof ch);if(file.gcount()!=std::streamsize(sizeof ch)){close();return false;}
                const uint64_t n=le32(ch+4),payload=pos+8;uint64_t end=0,next=0;
                if(!add_ok(payload,n,end)||!add_ok(end,n&1,next)||next>file_bytes){close();return false;}
                if(!std::memcmp(ch,"fmt ",4)){
                    if(have_fmt||n<16){close();return false;}
                    uint8_t fmt[16]{};file.read(reinterpret_cast<char*>(fmt),sizeof fmt);if(file.gcount()!=std::streamsize(sizeof fmt)){close();return false;}
                    const uint16_t tag=le16(fmt),channels=le16(fmt+2),align=le16(fmt+12),bits=le16(fmt+14);
                    const uint32_t sr=le32(fmt+4),byte_rate=le32(fmt+8);
                    if(tag!=1||channels!=nch||sr!=rate||bits!=16||align!=frame_bytes||byte_rate!=uint64_t(rate)*frame_bytes){close();return false;}
                    have_fmt=true;
                }else if(!std::memcmp(ch,"data",4)){
                    if(!have_fmt||have_data||n!=expected_bytes){close();return false;}
                    data_offset=payload;data_bytes=n;have_data=true;
                }
                pos=next;
            }
            if(!have_fmt||!have_data){close();return false;}
            cursor=0;block.reserve(128*1024);
            file.clear();file.seekg(std::streamoff(data_offset),std::ios::beg);if(!file){close();return false;}
            return true;
        }catch(...){close();return false;}
    }

    size_t read_frames(size_t wanted,uint32_t nch){
        const uint64_t frame_bytes=uint64_t(nch)*2;
        if(!file.is_open()||!frame_bytes||cursor>data_bytes)return 0;
        const uint64_t remain=(data_bytes-cursor)/frame_bytes;
        const size_t frames=size_t(std::min<uint64_t>(wanted,remain));
        const size_t bytes=frames*size_t(frame_bytes);block.resize(bytes);
        if(!bytes)return 0;
        file.read(reinterpret_cast<char*>(block.data()),std::streamsize(bytes));
        if(file.gcount()!=std::streamsize(bytes))return 0;
        cursor+=bytes;return frames;
    }
};
