import React, { useState, useEffect } from 'react';
import {
  LayoutDashboard, BellRing, FileText, ShieldAlert,
  Settings, BookOpen, LifeBuoy, Search, Bell,
  ChevronRight, Database, Cpu, Zap, AlertTriangle, Rocket
} from 'lucide-react';
import { motion, AnimatePresence } from 'motion/react';
import type { View } from './types';

// ── API helpers ───────────────────────────────────────────────────────────
const API_KEY = sessionStorage.getItem('to_key') ?? '';
const H = () => ({
  'X-API-Key': sessionStorage.getItem('to_key') ?? '',
  'Content-Type': 'application/json',
});

// ── Design tokens ─────────────────────────────────────────────────────────
const T = {
  bg:'#EFEBE2', surface:'#FFFFFF', card:'#FFFFFF', muted:'#F5F2EC',
  ink:'#1A1A1A', sub:'#6B6B6B', faint:'#9B9B9B',
  teal:'#2A9D8F', tealDim:'rgba(42,157,143,0.09)', tealBd:'rgba(42,157,143,0.2)',
  border:'rgba(26,26,26,0.08)', borderMd:'rgba(26,26,26,0.12)',
};

// ── Shared atoms ──────────────────────────────────────────────────────────
const Pill = ({ children, color=T.teal, bg=T.tealDim }: { children:React.ReactNode; color?:string; bg?:string }) => (
  <span style={{ display:'inline-flex', alignItems:'center', gap:4, padding:'3px 10px', borderRadius:9999, fontSize:10, fontWeight:700, letterSpacing:'0.06em', textTransform:'uppercase', color, background:bg }}>{children}</span>
);
const Dot = ({ color }: { color:string }) => (
  <span style={{ display:'inline-block', width:6, height:6, borderRadius:'50%', background:color, flexShrink:0 }} />
);
const Card = ({ children, style={} }: { children:React.ReactNode; style?:React.CSSProperties }) => (
  <div style={{ background:T.card, border:`1px solid ${T.border}`, borderRadius:16, boxShadow:'0 1px 6px rgba(26,26,26,0.04)', ...style }}>{children}</div>
);
const BtnDark = ({ children, onClick, style={} }: { children:React.ReactNode; onClick?:()=>void; style?:React.CSSProperties }) => (
  <button onClick={onClick} style={{ display:'inline-flex', alignItems:'center', gap:8, background:T.ink, color:'#fff', borderRadius:9999, padding:'8px 20px', fontSize:13, fontWeight:600, letterSpacing:'-0.01em', border:'none', cursor:'pointer', ...style }}>{children}</button>
);
const BtnOutline = ({ children, onClick, style={} }: { children:React.ReactNode; onClick?:()=>void; style?:React.CSSProperties }) => (
  <button onClick={onClick} style={{ display:'inline-flex', alignItems:'center', gap:8, background:'transparent', color:T.ink, border:`1px solid ${T.borderMd}`, borderRadius:9999, padding:'7px 18px', fontSize:13, fontWeight:600, letterSpacing:'-0.01em', cursor:'pointer', ...style }}>{children}</button>
);
const Lbl = ({ children }: { children:React.ReactNode }) => (
  <p style={{ fontSize:11, fontWeight:700, letterSpacing:'0.07em', textTransform:'uppercase', color:T.sub, marginBottom:8 }}>{children}</p>
);
const HRule = () => <div style={{ height:1, background:T.border, margin:'20px 0' }} />;

function PageHeader({ eyebrow, title, subtitle }: { eyebrow?:string; title:string; subtitle?:string }) {
  return (
    <div style={{ marginBottom:40 }}>
      {eyebrow && <p style={{ fontSize:11, fontWeight:700, letterSpacing:'0.08em', color:T.teal, textTransform:'uppercase', marginBottom:10 }}>{eyebrow}</p>}
      <h1 style={{ fontSize:38, fontWeight:800, letterSpacing:'-0.03em', color:T.ink, lineHeight:1.1, marginBottom:subtitle?10:0 }}>{title}</h1>
      {subtitle && <p style={{ fontSize:15, color:T.sub, lineHeight:1.65, maxWidth:560 }}>{subtitle}</p>}
      <div style={{ height:1, background:T.border, marginTop:20 }} />
    </div>
  );
}

function StatCard({ label, value, unit, sub }: { label:string; value:string; unit?:string; sub?:string }) {
  return (
    <Card style={{ padding:28 }}>
      <Lbl>{label}</Lbl>
      <div style={{ display:'flex', alignItems:'baseline', gap:6, margin:'4px 0' }}>
        <span style={{ fontSize:36, fontWeight:800, letterSpacing:'-0.03em', color:T.ink }}>{value}</span>
        {unit && <span style={{ fontSize:13, color:T.sub }}>{unit}</span>}
      </div>
      {sub && <p style={{ fontSize:12, color:T.faint, marginTop:4 }}>{sub}</p>}
    </Card>
  );
}

function NavItem({ active, icon, label, onClick }: { active:boolean; icon:React.ReactNode; label:string; onClick:()=>void }) {
  return (
    <button onClick={onClick} style={{ display:'flex', alignItems:'center', gap:10, padding:'9px 14px', borderRadius:8, border:'none', background:active?T.card:'transparent', color:active?T.ink:T.sub, fontWeight:active?600:400, fontSize:13, cursor:'pointer', width:'100%', textAlign:'left', letterSpacing:'-0.01em', boxShadow:active?'0 1px 4px rgba(26,26,26,0.06)':'none', transition:'all 0.15s' }}>
      <span style={{ color:active?T.teal:'#ABABAB', display:'flex', flexShrink:0 }}>{icon}</span>
      {label}
    </button>
  );
}

// ── Login screen ──────────────────────────────────────────────────────────
function LoginScreen({ onLogin }: { onLogin:(key:string)=>void }) {
  const [key, setKey] = useState('');
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState('');

  const handleLogin = async () => {
    if (!key.trim()) { setErr('Enter your API key.'); return; }
    setLoading(true); setErr('');
    try {
      const r = await fetch('/ops/auth/verify', { headers: { 'X-API-Key': key } });
      if (!r.ok) throw new Error('Invalid API key');
      sessionStorage.setItem('to_key', key);
      onLogin(key);
    } catch (e: any) {
      setErr(e.message || 'Login failed');
      setLoading(false);
    }
  };

  return (
    <div style={{ minHeight:'100vh', background:T.bg, display:'flex', alignItems:'center', justifyContent:'center', fontFamily:'Manrope, system-ui, sans-serif' }}>
      <div style={{ background:T.card, border:`1px solid ${T.border}`, borderRadius:20, padding:'48px 40px', width:400, boxShadow:'0 4px 40px rgba(26,26,26,0.08)' }}>
        <div style={{ display:'flex', alignItems:'center', gap:12, marginBottom:32 }}>
          <div style={{ width:36, height:36, borderRadius:9, background:T.teal, display:'flex', alignItems:'center', justifyContent:'center' }}>
            <Zap size={18} color="#fff"/>
          </div>
          <span style={{ fontWeight:800, fontSize:18, letterSpacing:'-0.03em' }}>TriageOps</span>
        </div>
        <h2 style={{ fontSize:24, fontWeight:800, letterSpacing:'-0.03em', marginBottom:6 }}>Welcome back</h2>
        <p style={{ fontSize:14, color:T.sub, marginBottom:28, lineHeight:1.5 }}>Enter your tenant API key to access the NOC dashboard.</p>
        {err && <div style={{ background:'rgba(239,68,68,0.08)', border:'1px solid rgba(239,68,68,0.2)', borderRadius:8, padding:'10px 14px', fontSize:13, color:'#DC2626', marginBottom:16 }}>{err}</div>}
        <label style={{ fontSize:12, fontWeight:700, color:T.sub, letterSpacing:'0.05em', display:'block', marginBottom:6 }}>API KEY</label>
        <input
          type="password" value={key}
          onChange={e=>setKey(e.target.value)}
          onKeyDown={e=>e.key==='Enter'&&handleLogin()}
          placeholder="Enter your API key…"
          style={{ width:'100%', padding:'10px 14px', border:`1px solid ${T.border}`, borderRadius:10, fontSize:13, fontFamily:'JetBrains Mono, monospace', marginBottom:6, outline:'none', background:T.card, color:T.ink }}
        />
        <p style={{ fontSize:12, color:T.faint, marginBottom:20 }}>Generate: <code style={{ fontFamily:'JetBrains Mono, monospace', background:T.muted, padding:'1px 5px', borderRadius:3 }}>make seed-key TENANT=my-noc</code></p>
        <BtnDark onClick={handleLogin} style={{ width:'100%', justifyContent:'center', opacity:loading?0.6:1 }}>
          {loading ? 'Connecting…' : 'Access Dashboard →'}
        </BtnDark>
        <p style={{ fontSize:11, color:T.faint, textAlign:'center', marginTop:20 }}>Read-only · Human approval required for all actions</p>
      </div>
    </div>
  );
}

// ── Root ──────────────────────────────────────────────────────────────────
export default function App() {
  const [apiKey, setApiKey] = useState(sessionStorage.getItem('to_key') ?? '');
  const [activeView, setActiveView] = useState<View>('dashboard');
  const nav = (v:View) => setActiveView(v);

  if (!apiKey) return <LoginScreen onLogin={k=>setApiKey(k)} />;

  return (
    <div style={{ background:T.bg, minHeight:'100vh', fontFamily:'Manrope, system-ui, sans-serif', color:T.ink }}>
      <header style={{ background:T.bg, borderBottom:`1px solid ${T.border}`, position:'sticky', top:0, zIndex:50, height:64, display:'flex', alignItems:'center', justifyContent:'space-between', padding:'0 40px' }}>
        <div style={{ display:'flex', alignItems:'center', gap:40 }}>
          <span style={{ fontWeight:800, fontSize:17, letterSpacing:'-0.03em' }}>TriageOps</span>
          <nav style={{ display:'flex', gap:28 }}>
            {(['dashboard','alerts','rules'] as View[]).map(v => (
              <button key={v} onClick={()=>nav(v)} style={{ background:'none', border:'none', cursor:'pointer', fontSize:14, fontWeight:activeView===v?600:400, color:activeView===v?T.ink:T.sub, paddingBottom:3, borderBottom:activeView===v?`2px solid ${T.ink}`:'2px solid transparent', transition:'all 0.15s', letterSpacing:'-0.01em' }}>
                {v==='dashboard'?'Dashboard':v==='alerts'?'Alert Stream':'Rules'}
              </button>
            ))}
          </nav>
        </div>
        <div style={{ display:'flex', alignItems:'center', gap:14 }}>
          <div style={{ position:'relative' }}>
            <Search size={13} style={{ position:'absolute', left:12, top:'50%', transform:'translateY(-50%)', color:T.sub }} />
            <input placeholder="Search…" style={{ background:'#fff', border:`1px solid ${T.border}`, borderRadius:9999, padding:'7px 16px 7px 32px', fontSize:13, outline:'none', width:180, color:T.ink }} />
          </div>
          <div style={{ width:34, height:34, borderRadius:'50%', background:T.teal, display:'flex', alignItems:'center', justifyContent:'center', color:'#fff', fontWeight:700, fontSize:13 }}>N</div>
          <button onClick={()=>{sessionStorage.clear();setApiKey('');}} style={{ background:'none', border:`1px solid ${T.border}`, borderRadius:9999, padding:'7px 14px', fontSize:12, color:T.sub, cursor:'pointer' }}>Sign out</button>
        </div>
      </header>

      <div style={{ display:'flex' }}>
        <aside style={{ width:216, flexShrink:0, position:'sticky', top:64, height:'calc(100vh - 64px)', background:'rgba(26,26,26,0.025)', borderRight:`1px solid ${T.border}`, display:'flex', flexDirection:'column', padding:'20px 10px', overflowY:'auto' }}>
          <nav style={{ flex:1, display:'flex', flexDirection:'column', gap:2 }}>
            <NavItem active={activeView==='dashboard'} icon={<LayoutDashboard size={15}/>} label="Dashboard"        onClick={()=>nav('dashboard')}/>
            <NavItem active={activeView==='alerts'}    icon={<BellRing size={15}/>}        label="Alert Stream"     onClick={()=>nav('alerts')}/>
            <NavItem active={activeView==='analysis'}  icon={<FileText size={15}/>}        label="Incident Analysis"onClick={()=>nav('analysis')}/>
            <NavItem active={activeView==='rules'}     icon={<ShieldAlert size={15}/>}     label="Suppression Rules"onClick={()=>nav('rules')}/>
            <NavItem active={activeView==='settings'}  icon={<Settings size={15}/>}        label="System Health"    onClick={()=>nav('settings')}/>
          </nav>
          <div style={{ borderTop:`1px solid ${T.border}`, paddingTop:12, display:'flex', flexDirection:'column', gap:2 }}>
            <NavItem active={false} icon={<BookOpen size={15}/>} label="Docs"    onClick={()=>window.open('/docs')}/>
            <NavItem active={false} icon={<LifeBuoy size={15}/>} label="Support" onClick={()=>{}}/>
          </div>
        </aside>

        <main style={{ flex:1, padding:'44px 52px', maxWidth:1060, width:'100%' }}>
          <AnimatePresence mode="wait">
            <motion.div key={activeView} initial={{opacity:0,y:8}} animate={{opacity:1,y:0}} exit={{opacity:0,y:-8}} transition={{duration:0.22,ease:'easeOut'}}>
              {activeView==='dashboard' && <DashboardView />}
              {activeView==='alerts'    && <AlertStreamView />}
              {activeView==='rules'     && <RulesView />}
              {activeView==='analysis'  && <AnalysisView />}
              {activeView==='settings'  && <DashboardView />}
            </motion.div>
          </AnimatePresence>
        </main>
      </div>

      <footer style={{ borderTop:`1px solid ${T.border}`, background:T.bg, padding:'24px 52px', display:'flex', justifyContent:'space-between', alignItems:'center' }}>
        <span style={{ fontSize:11, color:T.faint }}>TriageOps v1.1 · AI-powered NOC alert triage</span>
        <span style={{ fontSize:11, color:T.faint }}>Read-only · Human approval required</span>
      </footer>
    </div>
  );
}

// ── DASHBOARD ─────────────────────────────────────────────────────────────
function DashboardView() {
  const [stats, setStats] = useState<any>({});
  const [hotspots, setHotspots] = useState<any[]>([]);
  const [dist, setDist] = useState<any[]>([]);

  useEffect(() => {
    const h = H();
    fetch('/ops/stats', { headers:h }).then(r=>r.json()).then(setStats).catch(()=>{});
    fetch('/ops/stats/hotspots?limit=6', { headers:h }).then(r=>r.json()).then(setHotspots).catch(()=>{});
    fetch('/ops/stats/distribution', { headers:h }).then(r=>r.json()).then(setDist).catch(()=>{});
  }, []);

  const distColors: Record<string,string> = { CRITICAL:'#DC2626', NOISE:'#10B981', NEEDS_REVIEW:'#A78BFA' };
  const maxHot = Math.max(...hotspots.map(h=>h.alert_count||0), 1);

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:36 }}>
      <PageHeader eyebrow="Core Infrastructure" title="Dashboard" subtitle="AI-powered alert triage — last 24 hours." />

      <div style={{ display:'grid', gridTemplateColumns:'repeat(4,1fr)', gap:14 }}>
        <StatCard label="Alerts (24h)"       value={String(stats.alerts_24h??0)}       sub={`${stats.total_alerts??0} total`}/>
        <StatCard label="Critical (24h)"     value={String(stats.critical_24h??0)}     sub="Needs action"/>
        <StatCard label="Pending approvals"  value={String(stats.pending_approvals??0)} sub="Awaiting human"/>
        <StatCard label="Noise ratio"        value={stats.noise_ratio_24h!=null?`${Math.round(stats.noise_ratio_24h*100)}%`:'—'} sub="Signal vs noise"/>
      </div>

      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:16 }}>
        <Card style={{ padding:28 }}>
          <Lbl>Triage distribution (24h)</Lbl>
          {dist.length > 0 ? <>
            <div style={{ height:8, display:'flex', borderRadius:4, overflow:'hidden', gap:1, marginBottom:14 }}>
              {dist.map(x=><div key={x.decision} style={{ flex:x.count, background:distColors[x.decision]||T.teal }}/>)}
            </div>
            <div style={{ display:'flex', gap:16, flexWrap:'wrap' }}>
              {dist.map(x=>(
                <div key={x.decision} style={{ display:'flex', alignItems:'center', gap:6, fontSize:12, color:T.sub }}>
                  <div style={{ width:8, height:8, borderRadius:2, background:distColors[x.decision]||T.teal }}/>
                  {x.decision} <strong style={{ color:T.ink }}>{x.percentage}%</strong>
                </div>
              ))}
            </div>
          </> : <p style={{ fontSize:13, color:T.faint }}>No enriched alerts yet.</p>}
        </Card>

        <Card style={{ padding:28 }}>
          <Lbl>Top hosts by volume</Lbl>
          {hotspots.length > 0 ? hotspots.map(h=>(
            <div key={h.host} style={{ display:'flex', alignItems:'center', gap:10, marginBottom:10 }}>
              <span style={{ fontSize:12, fontFamily:'JetBrains Mono, monospace', color:T.teal, minWidth:160, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{h.host}</span>
              <div style={{ flex:1, height:4, background:'#E2DECF', borderRadius:99, overflow:'hidden' }}>
                <div style={{ height:'100%', width:`${Math.round(h.alert_count/maxHot*100)}%`, background:T.teal, borderRadius:99 }}/>
              </div>
              <span style={{ fontSize:12, color:T.sub, minWidth:24, textAlign:'right' }}>{h.alert_count}</span>
            </div>
          )) : <p style={{ fontSize:13, color:T.faint }}>No data yet.</p>}
        </Card>
      </div>

      <div style={{ display:'grid', gridTemplateColumns:'repeat(3,1fr)', gap:14 }}>
        <StatCard label="Escalations (24h)"  value={String(stats.escalations_24h??0)}   sub={`${stats.total_escalations??0} total`}/>
        <StatCard label="AI confidence avg"  value={stats.avg_confidence_24h!=null?`${Math.round(stats.avg_confidence_24h*100)}%`:'—'} sub="Last 24h"/>
        <StatCard label="LLM latency avg"    value={stats.avg_latency_ms_24h!=null?`${Math.round(stats.avg_latency_ms_24h)}ms`:'—'} sub="OpenAI response"/>
      </div>
    </div>
  );
}

// ── ALERT STREAM ──────────────────────────────────────────────────────────
function AlertStreamView() {
  const [alerts, setAlerts] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [filter, setFilter] = useState('');
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    const params = new URLSearchParams({ page:String(page), page_size:'20', ...(filter?{decision:filter}:{}) });
    fetch(`/ops/alerts?${params}`, { headers:H() })
      .then(r=>r.json())
      .then(d=>{ setAlerts(d.items??[]); setTotal(d.total??0); })
      .catch(()=>setAlerts([]))
      .finally(()=>setLoading(false));
  }, [page, filter]);

  const sevDot:Record<string,string>={ CRITICAL:'#DC2626', HIGH:'#D97706', MEDIUM:'#92400E', LOW:'#10B981' };
  const sevBg:Record<string,string>={ CRITICAL:'rgba(239,68,68,0.07)', HIGH:'rgba(245,158,11,0.07)', MEDIUM:'rgba(251,191,36,0.07)', LOW:'rgba(16,185,129,0.07)' };
  const decCol:Record<string,string>={ CRITICAL:T.teal, NOISE:'#10B981', NEEDS_REVIEW:'#A78BFA' };
  const decBg:Record<string,string>={ CRITICAL:T.tealDim, NOISE:'rgba(16,185,129,0.08)', NEEDS_REVIEW:'rgba(167,139,250,0.08)' };

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:24 }}>
      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-end' }}>
        <PageHeader eyebrow="Incident Monitor" title="Live Alert Stream" subtitle="AI-assessed anomalies from all connected monitoring sources."/>
        <div style={{ display:'flex', gap:8, paddingBottom:20 }}>
          <BtnOutline onClick={()=>setPage(1)}>Refresh</BtnOutline>
        </div>
      </div>

      <div style={{ display:'flex', gap:8, flexWrap:'wrap' }}>
        {[['All',''],['Critical','CRITICAL'],['Noise','NOISE'],['Review','NEEDS_REVIEW']].map(([label,val])=>(
          <button key={val} onClick={()=>{setFilter(val);setPage(1);}} style={{ padding:'5px 14px', borderRadius:9999, border:`1px solid ${T.border}`, background:filter===val?T.ink:'transparent', color:filter===val?'#fff':T.sub, fontSize:12, fontWeight:600, cursor:'pointer' }}>{label}</button>
        ))}
      </div>

      <Card style={{ overflow:'hidden' }}>
        {loading && <div style={{ padding:24, textAlign:'center', fontSize:13, color:T.faint }}>Loading…</div>}
        {!loading && alerts.length===0 && <div style={{ padding:48, textAlign:'center', fontSize:13, color:T.faint }}>No alerts found.</div>}
        {!loading && alerts.length>0 && (
          <table style={{ width:'100%', borderCollapse:'collapse' }}>
            <thead>
              <tr style={{ background:'#FAF9F6', borderBottom:`1px solid ${T.border}` }}>
                {['Alert ID','Host','Source','Severity','Decision','Confidence','Received'].map((h,i)=>(
                  <th key={h} style={{ padding:'12px 16px', textAlign:i>=5?'right':'left', fontSize:10, fontWeight:700, textTransform:'uppercase', letterSpacing:'0.08em', color:T.faint }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {alerts.map((a:any)=>(
                <tr key={a.id} style={{ borderBottom:`1px solid rgba(26,26,26,0.04)`, cursor:'pointer' }}
                  onMouseEnter={e=>(e.currentTarget.style.background=T.tealDim)}
                  onMouseLeave={e=>(e.currentTarget.style.background='transparent')}>
                  <td style={{ padding:'12px 16px', fontFamily:'JetBrains Mono, monospace', fontSize:11, fontWeight:700, color:T.teal }}>{a.alert_id}</td>
                  <td style={{ padding:'12px 16px', fontFamily:'JetBrains Mono, monospace', fontSize:11, color:T.ink, maxWidth:200, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{a.host}</td>
                  <td style={{ padding:'12px 16px' }}><span style={{ background:'rgba(26,26,26,0.05)', color:T.sub, fontSize:10, fontWeight:700, padding:'2px 8px', borderRadius:9999, textTransform:'uppercase' }}>{a.source}</span></td>
                  <td style={{ padding:'12px 16px' }}>
                    <span style={{ display:'inline-flex', alignItems:'center', gap:4, background:sevBg[a.severity]||T.tealDim, padding:'2px 8px', borderRadius:9999, fontSize:10, fontWeight:700, color:sevDot[a.severity]||T.teal, textTransform:'uppercase' }}>
                      <Dot color={sevDot[a.severity]||T.teal}/>{a.severity}
                    </span>
                  </td>
                  <td style={{ padding:'12px 16px' }}>
                    {a.triage_decision
                      ? <span style={{ display:'inline-flex', alignItems:'center', gap:4, background:decBg[a.triage_decision]||T.tealDim, padding:'2px 8px', borderRadius:9999, fontSize:10, fontWeight:700, color:decCol[a.triage_decision]||T.teal, textTransform:'uppercase' }}>{a.triage_decision.replace('_',' ')}</span>
                      : <span style={{ fontSize:11, color:T.faint }}>pending…</span>}
                  </td>
                  <td style={{ padding:'12px 16px', textAlign:'right' }}>
                    {a.confidence_score ? (
                      <div style={{ display:'flex', alignItems:'center', justifyContent:'flex-end', gap:8 }}>
                        <div style={{ width:56, height:4, background:'#E2DECF', borderRadius:99, overflow:'hidden' }}>
                          <div style={{ height:'100%', width:`${Math.round(parseFloat(a.confidence_score)*100)}%`, background:parseFloat(a.confidence_score)>0.9?T.teal:parseFloat(a.confidence_score)>0.7?'#F59E0B':'#EF4444', borderRadius:99 }}/>
                        </div>
                        <span style={{ fontFamily:'JetBrains Mono, monospace', fontSize:11 }}>{Math.round(parseFloat(a.confidence_score)*100)}%</span>
                      </div>
                    ) : <span style={{ fontSize:11, color:T.faint }}>—</span>}
                  </td>
                  <td style={{ padding:'12px 16px', textAlign:'right', fontSize:11, color:T.faint, fontFamily:'JetBrains Mono, monospace', whiteSpace:'nowrap' }}>
                    {a.received_at ? new Date(a.received_at).toLocaleString('en-GB',{month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit'}) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <div style={{ padding:'12px 16px', background:'#FAF9F6', borderTop:`1px solid ${T.border}`, display:'flex', justifyContent:'space-between', alignItems:'center' }}>
          <span style={{ fontSize:11, color:T.faint }}>{total} total alerts</span>
          <div style={{ display:'flex', gap:4 }}>
            <button onClick={()=>setPage(p=>Math.max(1,p-1))} disabled={page<=1} style={{ padding:'5px 12px', border:`1px solid ${T.border}`, borderRadius:6, background:'#fff', color:T.sub, fontSize:12, cursor:'pointer', opacity:page<=1?0.4:1 }}>← Prev</button>
            <button onClick={()=>setPage(p=>p+1)} disabled={alerts.length<20} style={{ padding:'5px 12px', border:`1px solid ${T.border}`, borderRadius:6, background:'#fff', color:T.sub, fontSize:12, cursor:'pointer', opacity:alerts.length<20?0.4:1 }}>Next →</button>
          </div>
        </div>
      </Card>
    </div>
  );
}

// ── RULES ─────────────────────────────────────────────────────────────────
function RulesView() {
  const [rules, setRules] = useState<any[]>([]);
  const [loading, setLoading] = useState(false);

  const load = () => {
    setLoading(true);
    fetch('/ops/suppression?page=1&page_size=25', { headers:H() })
      .then(r=>r.json()).then(d=>setRules(d.items??[])).catch(()=>setRules([])).finally(()=>setLoading(false));
  };
  useEffect(()=>{ load(); }, []);

  const toggleRule = async (id:string, val:string) => {
    await fetch(`/ops/suppression/${id}`, { method:'PATCH', headers:H(), body:JSON.stringify({is_active:val}) });
    load();
  };
  const deleteRule = async (id:string) => {
    if (!confirm('Delete this rule?')) return;
    await fetch(`/ops/suppression/${id}`, { method:'DELETE', headers:H() });
    load();
  };

  return (
    <div style={{ display:'flex', flexDirection:'column', gap:28 }}>
      <PageHeader eyebrow="Signal Intelligence" title="Suppression Rules Engine" subtitle="Pattern-based noise elimination — each rule encodes institutional knowledge."/>
      <Card style={{ overflow:'hidden' }}>
        <div style={{ padding:'14px 20px', background:'#FAF9F6', borderBottom:`1px solid ${T.border}`, display:'flex', justifyContent:'space-between', alignItems:'center' }}>
          <span style={{ fontSize:13, fontWeight:700 }}>Active Registry</span>
          <span style={{ fontSize:11, color:T.faint }}>{rules.length} rules</span>
        </div>
        {loading && <div style={{ padding:32, textAlign:'center', fontSize:13, color:T.faint }}>Loading…</div>}
        {!loading && rules.length===0 && <div style={{ padding:48, textAlign:'center', fontSize:13, color:T.faint }}>No suppression rules yet. Click Suppress in Slack to auto-create rules.</div>}
        {rules.map(r=>(
          <div key={r.id} style={{ display:'flex', alignItems:'flex-start', gap:16, padding:'18px 20px', borderBottom:`1px solid rgba(26,26,26,0.04)` }}>
            <div style={{ flex:1, minWidth:0 }}>
              <div style={{ display:'flex', alignItems:'center', gap:10, marginBottom:6 }}>
                <Pill>{r.match_field}</Pill><Pill bg="rgba(26,26,26,0.05)" color={T.sub}>{r.pattern_type}</Pill>
                <span style={{ fontSize:13, fontWeight:700, fontFamily:'JetBrains Mono, monospace', overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap' }}>{r.host_pattern||r.message_pattern||r.alert_id_prefix||'—'}</span>
              </div>
              {r.reason && <p style={{ fontSize:12, color:T.sub }}>{r.reason}</p>}
            </div>
            <div style={{ textAlign:'right', flexShrink:0 }}>
              <div style={{ fontSize:20, fontWeight:800, letterSpacing:'-0.02em' }}>{r.hit_count||0}</div>
              <div style={{ fontSize:10, color:T.faint }}>hits</div>
            </div>
            <div style={{ display:'flex', gap:6, flexShrink:0, alignItems:'center' }}>
              <button onClick={()=>toggleRule(r.id,r.is_active==='1'?'0':'1')} style={{ padding:'4px 10px', border:`1px solid ${T.border}`, borderRadius:6, background:'transparent', color:r.is_active==='1'?T.teal:T.faint, fontSize:11, cursor:'pointer', fontWeight:600 }}>{r.is_active==='1'?'Active':'Disabled'}</button>
              <button onClick={()=>deleteRule(r.id)} style={{ padding:'4px 10px', border:'1px solid rgba(239,68,68,0.2)', borderRadius:6, background:'transparent', color:'#DC2626', fontSize:11, cursor:'pointer' }}>Delete</button>
            </div>
          </div>
        ))}
      </Card>
    </div>
  );
}

// ── ANALYSIS (static enrichment viewer) ───────────────────────────────────
function AnalysisView() {
  const [alerts, setAlerts] = useState<any[]>([]);
  const [selected, setSelected] = useState<any>(null);

  useEffect(()=>{
    fetch('/ops/alerts?page=1&page_size=10&decision=CRITICAL', { headers:H() })
      .then(r=>r.json()).then(d=>setAlerts(d.items??[])).catch(()=>{});
  },[]);

  const loadDetail = (a:any) => {
    fetch(`/ops/alerts/${a.id}`, { headers:H() })
      .then(r=>r.json()).then(setSelected).catch(()=>setSelected(a));
  };

  return (
    <div style={{ display:'grid', gridTemplateColumns:'1fr 320px', gap:24 }}>
      <div style={{ display:'flex', flexDirection:'column', gap:16 }}>
        <PageHeader eyebrow="Enrichment" title="Incident Analysis" subtitle="Click any alert to view full AI enrichment and reasoning."/>
        {alerts.map(a=>(
          <Card key={a.id} style={{ padding:20, cursor:'pointer' }} onClick={()=>loadDetail(a)}>
            <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start' }}>
              <div>
                <span style={{ fontFamily:'JetBrains Mono, monospace', fontSize:11, color:T.teal, fontWeight:700 }}>{a.alert_id}</span>
                <p style={{ fontSize:14, fontWeight:700, marginTop:4 }}>{a.host}</p>
                <p style={{ fontSize:13, color:T.sub, marginTop:2 }}>{a.message?.slice(0,80)}{a.message?.length>80?'…':''}</p>
              </div>
              <ChevronRight size={16} style={{ color:T.faint }}/>
            </div>
          </Card>
        ))}
        {alerts.length===0 && <p style={{ fontSize:13, color:T.faint }}>No critical alerts found. Try changing the filter.</p>}
      </div>

      {selected && (
        <div style={{ position:'sticky', top:84, alignSelf:'start', display:'flex', flexDirection:'column', gap:14 }}>
          <Card style={{ padding:24 }}>
            <Lbl>Alert details</Lbl>
            <p style={{ fontSize:14, fontWeight:700, marginBottom:4 }}>{selected.host}</p>
            <p style={{ fontSize:12, color:T.sub, marginBottom:16 }}>{selected.message}</p>
            {selected.enrichment && <>
              <HRule />
              <Lbl>AI decision</Lbl>
              <Pill>{selected.enrichment.triage_decision}</Pill>
              <div style={{ marginTop:12 }}>
                <Lbl>Confidence</Lbl>
                <div style={{ height:6, background:'#E2DECF', borderRadius:99, overflow:'hidden', marginBottom:4 }}>
                  <div style={{ height:'100%', width:`${Math.round(parseFloat(selected.enrichment.confidence_score||0)*100)}%`, background:T.teal, borderRadius:99 }}/>
                </div>
                <span style={{ fontSize:12, color:T.sub }}>{Math.round(parseFloat(selected.enrichment.confidence_score||0)*100)}%</span>
              </div>
              {selected.enrichment.llm_reasoning && <>
                <HRule />
                <Lbl>AI reasoning</Lbl>
                <p style={{ fontSize:13, color:T.sub, lineHeight:1.7, fontStyle:'italic' }}>"{selected.enrichment.llm_reasoning?.slice(0,400)}"</p>
              </>}
              {selected.enrichment.suggested_action && <>
                <HRule />
                <Lbl>Suggested action</Lbl>
                <div style={{ background:'rgba(245,158,11,0.06)', border:'1px solid rgba(245,158,11,0.2)', borderRadius:8, padding:12 }}>
                  <p style={{ fontSize:10, fontWeight:800, color:'#D97706', letterSpacing:'0.06em', marginBottom:6 }}>⚠ AI suggestion — verify before executing</p>
                  <code style={{ fontSize:11, fontFamily:'JetBrains Mono, monospace', color:T.ink, whiteSpace:'pre-wrap' }}>{selected.enrichment.suggested_action}</code>
                </div>
              </>}
            </>}
          </Card>
        </div>
      )}
    </div>
  );
}

