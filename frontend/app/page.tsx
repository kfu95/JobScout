"use client";
import Onboarding from "./onboarding";
import { useCallback, useEffect, useState } from "react";

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
type Job = { id:number; company:string; provider:string; title:string; location:string; apply_url:string;
  is_open:boolean; score:number; score_reason:string; score_method:string; application_status:string;
  description?:string; packet?:Packet|null; first_seen:string; last_seen:string };
type Fact = { id:string; company?:string; role?:string; dates?:string; bullets?:string[] };
type Packet = { candidate_name:string; summary:string; skills:string[]; selected_facts:Fact[];
  why_company_draft:string; verification_notes:string[]; };
type Board = { id:number; company:string; provider:string; slug:string; last_error:string|null; last_success_at:string|null };
type Lead = { id:number; source:string; title:string; company:string; url:string };
type Stats = {boards:number;jobs:number;strong_matches:number;prepared:number;approved:number;reported:number};
const safeLink = (value:string) => value.startsWith("https://") ? value : "#";
async function api<T>(path:string, options?: RequestInit):Promise<T> {
  const r = await fetch(BASE + path, { cache: "no-store", ...options,
    headers: {"Content-Type":"application/json", ...(options?.headers || {})} });
  if (!r.ok) { const body = await r.json().catch(()=>({detail:r.statusText})); throw new Error(body.detail || `${r.status}`); }
  return r.json();
}
export default function Dashboard() {
  const [jobs,setJobs] = useState<Job[]>([]);
  const [boards,setBoards] = useState<Board[]>([]);
  const [leads,setLeads] = useState<Lead[]>([]);
  const [stats,setStats] = useState<Stats|null>(null);
  const [selected,setSelected] = useState<Job|null>(null);
  const [q,setQ] = useState(""); const [minScore,setMinScore] = useState(70);
  const [company,setCompany] = useState(""); const [provider,setProvider] = useState("greenhouse"); const [slug,setSlug] = useState("");
  const [leadUrl,setLeadUrl] = useState(""); const [leadTitle,setLeadTitle] = useState(""); const [leadCompany,setLeadCompany] = useState(""); const [leadSource,setLeadSource] = useState("LinkedIn");
  const [busy,setBusy] = useState(false); const [notice,setNotice] = useState("");
  const reloadJobs = useCallback(async()=>{
    setJobs(await api<Job[]>(`/api/jobs?min_score=${minScore}&q=${encodeURIComponent(q)}`));
  },[minScore,q]);
  const reloadOverview = useCallback(async()=>{
    const [b,l,s] = await Promise.all([
      api<Board[]>("/api/boards?limit=100"), api<Lead[]>("/api/leads"), api<Stats>("/api/stats")
    ]);
    setBoards(b);setLeads(l);setStats(s);
  },[]);
  const reload = useCallback(async()=>{await Promise.all([reloadJobs(),reloadOverview()]);},[reloadJobs,reloadOverview]);
  useEffect(()=>{reloadJobs().catch(e=>setNotice(e.message));},[reloadJobs]);
  useEffect(()=>{reloadOverview().catch(e=>setNotice(e.message));},[reloadOverview]);
  useEffect(()=>{const id = new URLSearchParams(window.location.search).get("job"); if(id && /^\d+$/.test(id)) selectJob(Number(id));},[]);
  async function selectJob(id:number){try{setSelected(await api<Job>(`/api/jobs/${id}`));}catch(e){setNotice(String(e));}}
  async function action(path:string,body?:object) {
    setBusy(true);setNotice("");
    try {
      await api(path,{method:"POST",body: body ? JSON.stringify(body) : undefined});
      await reload(); if(selected) await selectJob(selected.id);
      setNotice("Saved successfully.");
    } catch(e) {setNotice(e instanceof Error ? e.message : String(e));}
    finally {setBusy(false);}
  }
  async function addBoard(e:React.FormEvent){e.preventDefault();await action("/api/boards",{company,provider,slug});setSlug("");setCompany("");}
  async function addLead(e:React.FormEvent){e.preventDefault();await action("/api/leads",{source:leadSource,title:leadTitle,company:leadCompany,url:leadUrl});setLeadUrl("");setLeadTitle("");}
  function statusUpdate(status:string){
    const evidence = status === "confirmed" ? window.prompt("Paste a receipt ID or confirmation-email note (required):") : "";
    if(status === "confirmed" && !evidence) return;
    action(`/api/jobs/${selected?.id}/status`,{status,evidence});
  }
  return <main className="shell">
    <header><div><p className="eyebrow">PERSONAL JOB SEARCH</p><h1>Job Agent</h1><p className="muted">Discover, match, prepare. You always submit.</p></div>
      <div className="row"><button disabled={busy} onClick={()=>action("/api/digest/test")}>Send test digest</button>
      <button disabled={busy} onClick={()=>action("/api/collect")}>Run collectors now</button></div></header>
    {notice && <div role="status" className="notice">{notice}</div>}
    <div className="stats">{([
      ["Tracked boards",stats?.boards],["Open jobs",stats?.jobs],["Strong matches",stats?.strong_matches],
      ["Packets ready",stats?.prepared],["Approved",stats?.approved],["Reported sent",stats?.reported]
    ] as const).map(([label,n])=><div className="stat" key={label}><b>{n ?? "—"}</b><span>{label}</span></div>)}</div>
    <Onboarding onSaved={async()=>{await reload(); if(selected) await selectJob(selected.id);}}/>
    <div className="columns">
      <section className="panel feed"><div className="sectionTitle"><h2>Job feed</h2><span>{jobs.length} shown</span></div>
        <div className="filters"><input value={q} onChange={e=>setQ(e.target.value)} placeholder="Search roles, companies, locations…" />
          <label>Min score <select value={minScore} onChange={e=>setMinScore(Number(e.target.value))}><option value="0">All</option><option value="60">60+</option><option value="70">70+</option><option value="80">80+</option><option value="90">90+</option></select></label></div>
        <div className="jobList">{jobs.length===0 && <p className="muted">No matching jobs yet. Run collection to discover jobs.</p>}
          {jobs.map(j=><div key={j.id}><button className={`job ${selected?.id===j.id?"chosen":""}`} onClick={()=>selectJob(j.id)}>
            <span className="jobHead"><b>{j.title}</b><span className="score">{j.score}</span></span>
            <span>{j.company} · {j.location || "Location unspecified"}</span><small>{j.provider === "remoteok" ? "Remote OK" : j.provider} · {j.score_method} · {j.application_status.replaceAll("_"," ")}</small>
          </button>{j.provider === "adzuna" && <a href="https://www.adzuna.com/" target="_blank" rel="noopener noreferrer" style={{display:"inline-block", minWidth:116, minHeight:23}}>Adzuna</a>}</div>)}</div></section>
      <section className="panel detail">{selected ? <>
        <div className="sectionTitle"><h2>{selected.title}</h2><span className="score">{selected.score}</span></div>
        <p>{selected.company} · {selected.location}</p>
        {selected.provider === "adzuna" && <a href="https://www.adzuna.com/" target="_blank" rel="noopener noreferrer" style={{display:"inline-block", minWidth:116, minHeight:23}}>Adzuna</a>}
        <p className="muted">{selected.score_reason || "No score reasoning yet"}</p>
        <div className="row actions">
          <a className="button outline" href={safeLink(selected.apply_url)} target="_blank" rel="noopener noreferrer">Open job listing</a>
          <button disabled={busy || !selected.is_open} onClick={()=>action(`/api/jobs/${selected.id}/prepare`)}>Prepare packet</button>
          <button className="outline" disabled={busy} onClick={()=>action(`/api/jobs/${selected.id}/decision`,{decision:"skip"})}>Skip</button>
        </div>
        <p className="badge">Status: {selected.application_status.replaceAll("_"," ")}</p>
        {selected.packet && <div className="packet"><h3>Tailored packet — review required</h3>
          <p><b>{selected.packet.candidate_name}</b> — {selected.packet.summary}</p>
          <h4>Selected verified facts</h4>{selected.packet.selected_facts.map(f=><div key={f.id}><b>{f.role} · {f.company}</b><p className="muted">{f.dates}</p><ul>{(f.bullets||[]).map((b,i)=><li key={i}>{b}</li>)}</ul></div>)}
          <h4>Why this company (draft)</h4><p>{selected.packet.why_company_draft}</p>
          <h4>Verify before use</h4><ul>{selected.packet.verification_notes.map((note,i)=><li key={i}>{note}</li>)}</ul>
          <div className="row"><a className="button outline" href={`${BASE}/api/jobs/${selected.id}/resume.pdf`}>Download resume PDF</a>
            {selected.application_status==="prepared" && <button disabled={busy} onClick={()=>action(`/api/jobs/${selected.id}/decision`,{decision:"approve"})}>Approve materials</button>}
            {selected.application_status==="approved" && <button disabled={busy} onClick={()=>action(`/api/jobs/${selected.id}/reported`)}>I submitted (unverified)</button>}
          </div>
        </div>}
        {["submission_reported","confirmed","interview","rejected"].includes(selected.application_status) && <div className="row actions">
          <button className="outline" disabled={busy} onClick={()=>statusUpdate("confirmed")}>Confirm with receipt</button>
          <button className="outline" disabled={busy} onClick={()=>statusUpdate("interview")}>Interview</button>
          <button className="outline" disabled={busy} onClick={()=>statusUpdate("rejected")}>Rejected</button>
        </div>}
        <h3>Original listing</h3><div className="description">{selected.description || "No description returned."}</div>
      </> : <div className="empty"><h2>Select a job</h2><p className="muted">Review the listing, see scoring evidence, and approve its application packet.</p></div>}</section>
    </div>
    <div className="columns lower">
      <section className="panel"><h2>Tracked ATS boards</h2><p className="muted">Use the company token from its official Greenhouse, Lever, or Ashby jobs URL.</p>
        <form className="stack" onSubmit={addBoard}><input required value={company} placeholder="Company name" onChange={e=>setCompany(e.target.value)}/>
          <div className="row"><select value={provider} onChange={e=>setProvider(e.target.value)}><option>greenhouse</option><option>lever</option><option>ashby</option></select>
          <input required value={slug} placeholder="Board token / slug" onChange={e=>setSlug(e.target.value)}/><button disabled={busy}>Add board</button></div></form>
        <p className="muted">Showing the first {boards.length} of {stats?.boards ?? boards.length} boards.</p><ul className="boardList">{boards.map(b=><li key={b.id}><b>{b.company}</b> · {b.provider}/{b.slug}<small>{b.last_error ? `Last error: ${b.last_error}` : b.last_success_at ? "Last fetch succeeded" : "Not polled yet"}</small></li>)}</ul>
      </section>
      <section className="panel"><h2>LinkedIn / Built In leads</h2><p className="muted">Manual intake only. JobScout does not scrape protected job boards.</p>
        <form className="stack" onSubmit={addLead}><div className="row"><select value={leadSource} onChange={e=>setLeadSource(e.target.value)}><option>LinkedIn</option><option>Built In</option><option>Other</option></select>
          <input placeholder="Company" value={leadCompany} onChange={e=>setLeadCompany(e.target.value)}/></div>
          <input required placeholder="Role title" value={leadTitle} onChange={e=>setLeadTitle(e.target.value)}/>
          <input required type="url" placeholder="https://… posting URL" value={leadUrl} onChange={e=>setLeadUrl(e.target.value)}/><button disabled={busy}>Save lead</button></form>
        <ul className="boardList">{leads.map(l=><li key={l.id}><a href={safeLink(l.url)} target="_blank" rel="noopener noreferrer">{l.title}</a> · {l.company}<small>{l.source}</small></li>)}</ul>
      </section>
    </div>
    <footer>Local-only starter · No automatic submission · Never expose port 8000 or 3000 to the public internet without authentication.</footer>
  </main>;
}
