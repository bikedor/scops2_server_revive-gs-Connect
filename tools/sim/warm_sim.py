import re,sys,datetime as dt,bisect,collections
MARGIN=20   # seconds a reconnect needs the hole to stay open (host may still be loading the map)
def T(s): return dt.datetime.strptime(s,'%Y-%m-%d %H:%M:%S.%f').timestamp()
def load(path,host):
    links=[];steers=[];pings=[]
    hq=re.escape(host)
    rl=re.compile(r'^\[(\S+ \S+)\] RELAY links host '+hq+r': (.*)')
    rs=re.compile(r'^\[(\S+ \S+)\] HOLE steer #\d+: '+hq+r' socket 1 -> ping port (\d+)')
    rp=re.compile(r'^\[(\S+ \S+)\]\s+HOLE PING #\d+: '+hq+r':\d+ reached port (\d+)')
    ent=re.compile(r'(\d+\.\d+\.\d+\.\d+):(\d+)@(\d+) in=(\d+) out=(\d+) idle=(\d+)s')
    with open(path,errors='replace') as f:
        for l in f:
            if host not in l: continue
            m=rl.match(l)
            if m:
                links.append((T(m.group(1)),[(m2.group(1),int(m2.group(2)),int(m2.group(3)),int(m2.group(4)),int(m2.group(5)),int(m2.group(6))) for m2 in ent.finditer(m.group(2))])); continue
            m=rs.match(l)
            if m: steers.append((T(m.group(1)),int(m.group(2)))); continue
            m=rp.match(l)
            if m: pings.append((T(m.group(1)),int(m.group(2))))
    return links,steers,pings

def sessions(links,gap=300):
    out=[];cur=[]
    for x in links:
        if cur and x[0]-cur[-1][0]>gap: out.append(cur); cur=[]
        cur.append(x)
    if cur: out.append(cur)
    return out

def analyse(links,steers,pings,L,warm,full=3,warm_gap=26,warm_age=40,warm_max=900,reloads=(),move=False):
    t0=links[0][0]-60; t1=links[-1][0]
    st=[s for s in steers if t0<=s[0]<=t1]; pg=[p for p in pings if t0-200<=p[0]<=t1]
    # host data traffic per hole (out>0 in a summary window)
    data=collections.defaultdict(list)
    for t,es in links:
        for ip,pt,hole,i,o,idle in es:
            if o>0: data[hole].append(t)
    # endpoint activity
    act=collections.defaultdict(list); hole_of={}
    for t,es in links:
        for ip,pt,hole,i,o,idle in es:
            if i>0: act[(ip,pt)].append(t-idle); hole_of[(ip,pt,t)]=hole
    # wake events: endpoint active again after >=60 s silence
    wakes=[]
    for ep,ts in act.items():
        ts=sorted(set(ts))
        for a,b in zip(ts,ts[1:]):
            if b-a>=60: wakes.append((b-15,ep))
    # ping schedule: phase from first logged ping
    if not pg: return None
    phase=pg[0][0]
    ticks=[]; k=int((t0-phase)//25)
    while phase+25*k<=t1:
        ticks.append(phase+25*k); k+=1
    # steer sequence real (+warm)
    steer_events=sorted(st)
    host_ping=collections.defaultdict(list)
    target=pg[0][1]; si=0; last_steer=t0; warm_prev=False; n_warm=0; used_reload=set()
    ep_last={}   # endpoint -> last activity time (from act)
    acts=sorted((t,ep) for ep,ts in act.items() for t in ts)
    ai=0
    # endpoint -> hole (latest seen)
    ep_hole={}
    lk=iter(links); nxt_link=next(lk,None)
    def last_traffic(h,t):
        a=data.get(h,[]); i=bisect.bisect_left(a,t)-1; d=a[i] if i>=0 else -1e18
        b=host_ping.get(h,[]); j=bisect.bisect_left(b,t)-1; p=b[j] if j>=0 else -1e18
        return max(d,p)
    for tk in ticks:
        # advance info up to tk
        while ai<len(acts) and acts[ai][0]<=tk: ep_last[acts[ai][1]]=acts[ai][0]; ai+=1
        while nxt_link and nxt_link[0]<=tk:
            for ip,pt,hole,i,o,idle in nxt_link[1]: ep_hole[(ip,pt)]=hole
            nxt_link=next(lk,None)
        while si<len(steer_events) and steer_events[si][0]<=tk-1:
            target=steer_events[si][1]; last_steer=steer_events[si][0]; si+=1; warm_prev=False
        urgent=False
        if warm and reloads:
            rr=[r for r in reloads if tk-45<=r<=tk and r not in used_reload]
            if rr: urgent=True; used_reload.update(rr)
        if warm and (urgent or tk-last_steer>=warm_gap):
            # warm_pick
            by_ip=collections.defaultdict(list)
            for ep,h in ep_hole.items():
                lt=ep_last.get(ep)
                if lt is not None and tk-lt<=warm_max: by_ip[ep[0]].append((lt,ep,h))
            assigned=set(ep_hole.values()); pick=None
            if (not urgent) and by_ip and len(by_ip)<full and warm_prev and target in assigned:
                spares=[h for h in range(45001,45013) if h not in assigned]
                if spares: pick=max(spares,key=lambda h:last_traffic(h,tk)); warm_prev=False
            if pick is None and by_ip:
                c=[]
                for ip,lst in by_ip.items():
                    for lt,ep,h in sorted(lst):
                        if tk-lt<=5: continue
                        if h==target: break
                        age=tk-last_traffic(h,tk)
                        if age>=warm_age: c.append((age,h))
                        break
                if c: pick=max(c)[1]; warm_prev=True
            if pick is not None:
                target=pick; last_steer=tk-0.5; n_warm+=1
                continue   # the steer re-targets: this tick still... (steer lands before next tick)
        host_ping[target].append(tk)
    res={'wakes':len(wakes),'dead':0,'ages':[], 'n_warm':n_warm,'new':0,'new_ready':0}
    firsts=[(min(ts)-15,ep) for ep,ts in act.items()]
    for w,ep in firsts:
        res['new']+=1
        # a hole pinged by the host in the 50 s before the new link (the spare the relay hands out)
        if any(any(w-50<=p<=w for p in host_ping[h]) for h in host_ping): res['new_ready']+=1
    reserved={}
    for w,ep in sorted(wakes):
        # hole the endpoint uses at wake: first summary at/after wake listing it
        h=None
        for t,es in links:
            if t>=w:
                for ip,pt,hole,i,o,idle in es:
                    if (ip,pt)==ep: h=hole; break
                if h: break
        if h is None: continue
        age=w-last_traffic(h,w)
        res['ages'].append(age)
        if age+MARGIN>L:
            if move:
                # move this reconnect to another hole that is open (host sent recently) and has no live link on it
                live=set()
                for t,es in links:
                    if w-35<=t<=w+65:
                        for ip,pt,hole,i,o,idle in es:
                            # another link sending on that hole around the reconnect (from 5 s before to 30 s after)
                            if i>0 and (ip,pt)!=ep and t-idle>=w-5 and t-30<=w+30: live.add(hole)
                ok=[x for x in range(45001,45013) if x!=h and x not in live and reserved.get(x,-1)<w and w-last_traffic(x,w)+MARGIN<=L]
                if ok:
                    best=max(ok,key=lambda x:last_traffic(x,w)); reserved[best]=w+60; res['moved']=res.get('moved',0)+1; continue
            res['dead']+=1
    return res

def load_reloads(path,host):
    out=[]
    rx=re.compile(r'^\[(\S+ \S+)\] ROUTER_WM LOBBY_MSG subtype=(17|45) from=\S+@'+re.escape(host)+':')
    with open(path,errors='replace') as f:
        for l in f:
            if 'subtype=17' in l or 'subtype=45' in l:
                m=rx.match(l)
                if m: out.append(T(m.group(1)))
    return out
if __name__=='__main__':
    path,host,rpath=sys.argv[1],sys.argv[2],sys.argv[3]
    links,steers,pings=load(path,host)
    reloads=load_reloads(rpath,host)
    for s in sessions(links):
        if len(s)<10: continue
        a=dt.datetime.fromtimestamp(s[0][0]).strftime('%m-%d %H:%M'); b=dt.datetime.fromtimestamp(s[-1][0]).strftime('%H:%M')
        line=[f"{host} {a}-{b} ({len(s)} summaries)"]
        for L in (60,120):
            r0=analyse(s,steers,pings,L,False); r1=analyse(s,steers,pings,L,True); r2=analyse(s,steers,pings,L,False,move=True); r3=analyse(s,steers,pings,L,True,move=True)
            if r0 is None: continue
            line.append(f"L={L}s: reconnects {r0['wakes']}, dead: now {r0['dead']} / warm {r1['dead']} / move {r2['dead']} / warm+move {r3['dead']}")
        nr=sum(1 for r in reloads if s[0][0]<=r<=s[-1][0])

        print(' | '.join(line))
