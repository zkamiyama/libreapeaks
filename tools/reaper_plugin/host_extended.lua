-- Extended normal-operation host acceptance. Never call peak-build extension APIs.
local root=assert(os.getenv('LRPK_CASE'))
local media=assert(os.getenv('LRPK_MEDIA'))
local op=os.getenv('LRPK_ACTION') or 'import'
local plugin=os.getenv('LRPK_EXPECT_PLUGIN')=='1'
local f=assert(io.open(root..'/result.txt','w'))
local closed=false
local function log(k,v) if not closed then f:write(k,'=',tostring(v),'\n');f:flush() end end
local function quit(err)
  if closed then return end
  if err then log('error',err) end
  local ok,e=pcall(reaper.Main_SaveProjectEx,0,root..'/saved.rpp',0)
  if not ok then log('save_error',e) end
  log('finished',true);f:close();closed=true;reaper.Main_OnCommand(40004,0)
end
local function normalize(s) return tostring(s or ''):gsub('%z',''):gsub('%s+',' '):match('^%s*(.-)%s*$'):lower() end
local function main()
  log('version',reaper.GetAppVersion());log('resource',reaper.GetResourcePath())
  log('plugin',reaper.APIExists('RPKX_Status'))
  local by_id,by_name={},{}
  for i=0,65535 do
    local id=reaper.kbd_enumerateActions(0,i)
    if not id or id<=0 then break end
    local name=reaper.kbd_getTextFromCmd(id,0) or ''
    by_id[id]=name;by_name[normalize(name)]=id
  end
  local af=assert(io.open(root..'/actions.txt','w'))
  for id,name in pairs(by_id) do
    local n=normalize(name)
    if n:find('peak') or n:find('offline') or n:find('online') or n:find('glue') or n:find('render') or n:find('record') or n:find('loudness') or n:find('spectr') then
      af:write(id,'\t',name,'\n')
    end
  end
  af:close()
  local function action(names,candidates)
    local id
    for _,name in ipairs(names) do if by_name[normalize(name)] then id=by_name[normalize(name)];break end end
    if not id then
      for _,candidate in ipairs(candidates or {}) do
        local actual=normalize(reaper.kbd_getTextFromCmd(candidate,0))
        for _,name in ipairs(names) do if actual==normalize(name) then id=candidate;break end end
        if id then break end
      end
    end
    if not id then error('Missing host action: '..table.concat(names,' / ')) end
    log('action',reaper.kbd_getTextFromCmd(id,0));log('action_id',id)
    reaper.Main_OnCommand(id,0);log('action_returned',id)
  end
  local function rebuild() action({'Peaks: Rebuild all peaks','Peaks: Rebuild peaks'},{40048,41101}) end
  local function base_source(item)
    if not item then return nil end
    local take=reaper.GetActiveTake(item);if not take then return nil end
    local src=reaper.GetMediaItemTake_Source(take);if not src then return nil end
    for i=1,16 do local p=reaper.GetMediaSourceParent(src);if not p then break end;src=p end
    return src
  end
  local function source_file(src)
    if not src then return '' end
    local ok,name=pcall(reaper.GetMediaSourceFileName,src,'')
    if ok then return tostring(name or '') end
    return ''
  end
  local function find_source(prefer_new)
    local fallback_src,fallback_name=nil,''
    for i=0,reaper.CountMediaItems(0)-1 do
      local src=base_source(reaper.GetMediaItem(0,i))
      if src then
        local name=source_file(src)
        if not fallback_src then fallback_src,fallback_name=src,name end
        if prefer_new and name~='' and name~=media then return src,name end
      end
    end
    return fallback_src,fallback_name
  end
  log('phase','import_begin');reaper.InsertMedia(media,0);log('phase','import_returned')
  local item=reaper.GetMediaItem(0,0);if item then reaper.SetMediaItemSelected(item,true) end
  local track=item and reaper.GetMediaItemTrack(item) or nil
  if track then reaper.SetOnlyTrackSelected(track) end
  local initial=reaper.time_precise();local acted=false;local action_time=initial
  local state='wait';local record_started=0;local last=-999
  local creates=(op=='glue' or op=='render' or op=='record')
  local tick
  tick=function()
    local ok,err=xpcall(function()
      local now=reaper.time_precise();local age=now-initial
      if state=='recording' then
        if now-record_started>1.5 then
          action({'Transport: Stop (save all recorded media)','Transport: Stop'},{40667,1016});state='post';action_time=reaper.time_precise();log('record_stopped',true)
        end
        reaper.defer(tick);return
      end
      if state=='normal-wait' then
        reaper.UpdateArrange()
        if now-action_time>0.75 then rebuild();state='post';action_time=reaper.time_precise() end
        reaper.defer(tick);return
      end
      local src,name=find_source(acted and creates)
      if not src then
        if age>12 then error(acted and 'No resulting source after action' or 'No imported source') end
        reaper.defer(tick);return
      end
      local st=plugin and reaper.APIExists('RPKX_Status') and reaper.RPKX_Status(src) or 0
      if st~=last then log('status',st);last=st end
      if not acted and age>1 and (not plugin or st==2 or st==0 or st==-1 or st==-2) then
        acted=true;log('pre_action_status',st)
        if op=='manual' then rebuild()
        elseif op=='spectral' then action({'Peaks: Toggle spectral peaks'},{42073})
        elseif op=='loudness' then action({'Peaks: Toggle show graph of momentary loudness (LUFS-M)'},{43146})
        elseif op=='normal' then
          action({'Peaks: Show normal peaks'},{42301});state='normal-wait';action_time=reaper.time_precise();reaper.defer(tick);return
        elseif op=='online' then
          action({'Item: Set all media offline'},{40100});rebuild();action({'Item: Set all media online'},{40101})
        elseif op=='glue' then action({'Item: Glue items, ignoring time selection','Item: Glue items'},{40362})
        elseif op=='render' then action({'Item: Render items to new take','Item: Render items as new take','Item: Render items to new take (preserve source type)','Item: Render items as new take (preserve source type)'},{})
        elseif op=='record' then
          if not track then error('No track for record setup') end
          reaper.Main_SaveProjectEx(0,root..'/record.rpp',0)
          action({'Track: Set track record mode to output (stereo)'},{40497})
          reaper.SetMediaTrackInfo_Value(track,'I_RECARM',1)
          reaper.SetMediaTrackInfo_Value(track,'I_RECMONITEMS',1)
          reaper.SetEditCurPos(0,false,false)
          action({'Transport: Record'},{1013});state='recording';record_started=reaper.time_precise();log('record_started',true)
          reaper.defer(tick);return
        end
        state='post';action_time=reaper.time_precise();reaper.defer(tick);return
      end
      reaper.UpdateArrange()
      if acted and state=='post' then
        src,name=find_source(creates)
        if creates and (not src or name=='' or name==media) then
          if now-action_time>15 then error('Normal action did not create a new file source') end
          reaper.defer(tick);return
        end
        st=plugin and reaper.APIExists('RPKX_Status') and reaper.RPKX_Status(src) or 0
        if st~=last then log('status',st);last=st end
        -- Newly-created media may already have a valid cache produced by the
        -- render/record/glue sink, so an idle wrapped source is a valid terminal
        -- creation state. It is NOT counted as plugin generation by Python.
        local terminal=(st==2 or st==-1 or st==-2 or (creates and st==0))
        if now-action_time>2 and (not plugin or terminal) then
          log('type',reaper.GetMediaSourceType(src,''));log('final_status',st);log('source_file',name)
          log('peak_read',reaper.GetPeakFileNameEx(name,'',false));log('peak_write',reaper.GetPeakFileNameEx(name,'',true))
          local arr=reaper.new_array(64);local n=reaper.PCM_Source_GetPeaks(src,100,0,2,16,0,arr)
          log('peak_count',n & 0xfffff);log('item_count',reaper.CountMediaItems(0));quit();return
        end
      end
      if age>70 then error('Extended host scheduler timeout status='..st..' state='..state) end
      reaper.defer(tick)
    end,debug.traceback)
    if not ok then quit(err) end
  end
  reaper.defer(tick)
end
local ok,err=xpcall(main,debug.traceback)
if not ok then quit(err) end
