"use client";
import { useEffect, useState } from "react";

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
type Fact = {id:string; role:string; company:string; dates:string; bullets:string[]; skills:string[]; verified:boolean};
type Preferences = {target_titles:string[]; role_families:string[]; seniority:string[]; locations:string[]; work_modes:string[]; salary_floor:number|null; currency:"USD"; excluded_companies:string[]; excluded_keywords:string[]};
const fields: [keyof Preferences, string][] = [["target_titles","Target titles"],["role_families","Role families"],["seniority","Seniority"],["locations","Locations"],["work_modes","Work modes (remote, hybrid, onsite)"],["excluded_companies","Excluded companies"],["excluded_keywords","Excluded keywords"]];
async function request(path:string, options?:RequestInit) {
  const response = await fetch(BASE + path, options);
  if (!response.ok) { const body = await response.json(); throw new Error(body.detail || response.statusText); }
  return response.json();
}
export default function Onboarding({onSaved}:{onSaved:()=>void|Promise<void>}) {
  const [facts,setFacts] = useState<Fact[]>([]); const [prefs,setPrefs] = useState<Preferences|null>(null); const [notice,setNotice] = useState("");
  async function refresh() { const data = await request("/api/onboarding"); setFacts(data.facts); setPrefs(data.preferences); }
  useEffect(()=>{refresh().catch(e=>setNotice(e.message));},[]);
  async function upload(e:React.FormEvent<HTMLFormElement>) { e.preventDefault(); try { const form = e.currentTarget; await request("/api/onboarding/resume",{method:"POST",body:new FormData(form)}); form.reset(); await refresh(); setNotice("Resume imported. Review each draft fact."); } catch(e) {setNotice(String(e));} }
  async function saveFact(fact:Fact) { try { await request(`/api/onboarding/facts/${fact.id}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(fact)}); await onSaved(); setNotice("Fact saved."); } catch(e) {setNotice(String(e));} }
  async function savePrefs(e:React.FormEvent) {e.preventDefault(); try {await request("/api/onboarding/preferences",{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(prefs)}); await onSaved(); setNotice("Preferences saved.");} catch(e) {setNotice(String(e));} }
  function edit(index:number, change:Partial<Fact>) {setFacts(facts.map((f,i)=>i===index?{...f,...change}:f));}
  return <section className="panel onboarding"><h2>Resume and preferences</h2><p className="muted">Imported facts are drafts until you verify them. Review each field before using it in a resume.</p>
    {notice && <p role="status">{notice}</p>}
    <form className="row" onSubmit={upload}><input aria-label="Resume file" name="file" type="file" accept=".pdf,.docx" required/><button>Upload resume</button></form>
    {facts.map((fact,i)=><div className="packet stack" key={fact.id}><strong>{fact.verified ? "Verified" : "Draft"}</strong>
      {(["role","company","dates"] as const).map(key=><label key={key}>{key}<input value={fact[key]} onChange={e=>edit(i,{[key]:e.target.value,verified:false})}/></label>)}
      <label>Bullets (one per line)<textarea value={fact.bullets.join("\n")} onChange={e=>edit(i,{bullets:e.target.value.split("\n"),verified:false})}/></label>
      <label>Skills (comma separated)<input value={fact.skills.join(", ")} onChange={e=>edit(i,{skills:e.target.value.split(",").map(s=>s.trim()),verified:false})}/></label>
      <label><input type="checkbox" checked={fact.verified} onChange={e=>edit(i,{verified:e.target.checked})}/> I verified this fact</label><button type="button" onClick={()=>saveFact(fact)}>Save fact</button></div>)}
    {prefs && <form className="stack" onSubmit={savePrefs}><h3>Job preferences</h3>{fields.map(([key,label])=><label key={key}>{label} (comma separated)<input value={(prefs[key] as string[]).join(", ")} onChange={e=>setPrefs({...prefs,[key]:e.target.value.split(",").map(s=>s.trimStart())})}/></label>)}
      <label>Annual salary floor (USD)<input type="number" min="0" value={prefs.salary_floor ?? ""} onChange={e=>setPrefs({...prefs,salary_floor:e.target.value ? Number(e.target.value) : null})}/></label><button>Save preferences</button></form>}
  </section>;
}
