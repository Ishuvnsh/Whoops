(()=> {
"use strict";
const $=id=>document.getElementById(id);
let latest=null, busy=false;
const descriptions={
 basic:"Compatibility-first minimisation of screen dimensions and language detail.",
 balanced:"Broader minimisation including canvas, WebGL, exact timezone and hardware details where collected.",
 strong:"Strictest supported research representation. User-agent and platform details are also suppressed; some sites may behave differently."
};
const profile=()=>document.querySelector('input[name="profile"]:checked')?.value||"basic";
const pretty=value=>value===null||value===undefined||value===""?"Not available":typeof value==="object"?JSON.stringify(value):String(value);
function setStatus(id,message,error=false){const el=$(id);el.textContent=message;el.classList.toggle("error",error);}
async function loadLatest(){
 if(busy)return;
 $("reload-latest").disabled=true;
 setStatus("source-status","Loading latest saved WHOOPS record…");
 $("apply-protection").disabled=true;
 $("protection-results").hidden=true;
 try{
  const response=await fetch("/protection/latest",{cache:"no-store"});
  const data=await response.json();
  if(!response.ok)throw new Error(data.error||"Could not retrieve the latest fingerprint.");
  latest=data.record;
  $("source-id").textContent=latest.record_id||"ID unavailable in legacy record";
  $("source-time").textContent=latest.timestamp?new Date(latest.timestamp).toLocaleString():"Timestamp unavailable";
  $("source-json").textContent=JSON.stringify(latest,null,2);
  $("source-meta").hidden=false;$("source-json").hidden=false;
  setStatus("source-status","Latest collected fingerprint loaded from the existing WHOOPS CSV dataset.");
  $("apply-protection").disabled=false;
 }catch(error){
  latest=null;$("source-meta").hidden=true;$("source-json").hidden=true;
  setStatus("source-status",error.message,true);
 }finally{$("reload-latest").disabled=false;}
}
function render(data){
 $("protection-results").hidden=false;
 $("profile-result").textContent=({basic:"BASIC",balanced:"BALANCED",strong:"STRONG"})[data.profile]||data.profile;
 $("original-json").textContent=JSON.stringify(data.original,null,2);
 $("protected-json").textContent=JSON.stringify(data.protected,null,2);
 const summary=data.summary||{};
 $("count-examined").textContent=summary.signals_examined??0;
 $("count-modified").textContent=summary.signals_modified??0;
 $("count-restricted").textContent=summary.signals_restricted??0;
 $("count-unchanged").textContent=summary.signals_unchanged??0;
 $("count-unsupported").textContent=summary.signals_unsupported??0;
 $("protection-id").textContent=data.record_id;
 $("protection-time").textContent=new Date(data.timestamp).toLocaleString();
 const list=$("policy-list");list.replaceChildren();
 if(data.policies_applied?.length){data.policies_applied.forEach(policy=>{const li=document.createElement("li");li.textContent=policy;list.append(li);});}
 else {const li=document.createElement("li");li.textContent="No supported fields required a transformation for this profile.";list.append(li);}
 const body=$("signal-rows");body.replaceChildren();
 Object.keys(data.signal_status||{}).sort().forEach(key=>{
  const status=data.signal_status[key],tr=document.createElement("tr");
  const values=[key,pretty(data.original[key]),pretty(data.protected[key])];
  values.forEach(value=>{const td=document.createElement("td");td.textContent=value;tr.append(td);});
  const td=document.createElement("td"),span=document.createElement("span");
  span.className="signal-status "+status;span.textContent=({modified:"MODIFIED",restricted:"RESTRICTED",unchanged:"UNCHANGED",unsupported:"UNSUPPORTED",partial:"PARTIAL"})[status]||status.toUpperCase();
  td.append(span);tr.append(td);body.append(tr);
 });
 const before=data.metrics_before||{},after=data.metrics_after||{};
 if(before.records>=2){
  $("metrics-text").textContent=`Across ${before.records} collected record(s), exact-row uniqueness was ${before.uniqueness_percent}% before applying this profile to the cohort and ${after.uniqueness_percent}% after applying the same profile to each cohort record. Exact-row Shannon entropy: ${before.shannon_entropy_bits} bits before and ${after.shannon_entropy_bits} bits after. Collision rate: ${before.collision_rate_percent}% before and ${after.collision_rate_percent}% after. These are local dataset statistics, not evidence of anonymity or browser-level blocking.`;
 }else{$("metrics-text").textContent="No collected cohort was available for a meaningful dataset-level calculation. No uniqueness, entropy or collision figures have been invented.";}
 const ab=data.feature_ablation||{}, abRows=$("ablation-rows");abRows.replaceChildren();
 $("ablation-note").textContent=ab.note||"Feature-ablation analysis unavailable.";
 if(ab.available){
  $("ablation-uniqueness").textContent=`Existing engine uniqueness: ${ab.original_uniqueness}% original → ${ab.protected_uniqueness}% after applying ${data.profile} to the same collected cohort.`;
  const original=ab.original_results||[], protectedResults=ab.protected_results||[];
  original.forEach(row=>{
   const match=protectedResults.find(x=>x["Category Removed"]===row["Category Removed"]);
   const tr=document.createElement("tr");
   [row["Category Removed"],pretty(row["Contribution (percentage points)"]),pretty(match?.["Contribution (percentage points)"])].forEach(value=>{const td=document.createElement("td");td.textContent=value;tr.append(td);});
   abRows.append(tr);
  });
 }else{$("ablation-uniqueness").textContent="No feature-ablation figures available."; }
 $("protection-results").scrollIntoView({behavior:"smooth",block:"start"});
}
async function applyProtection(){
 if(busy||!latest)return;
 busy=true;const button=$("apply-protection");button.disabled=true;
 setStatus("apply-status","Applying selected policy to the latest saved record…");
 try{
  const response=await fetch("/protection/apply",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({profile:profile()})});
  const data=await response.json();
  if(!response.ok)throw new Error(data.error||"Protection operation failed.");
  render(data);
  setStatus("apply-status",data.notice);
 }catch(error){setStatus("apply-status",error.message,true);}
 finally{busy=false;button.disabled=!latest;}
}
document.querySelectorAll('input[name="profile"]').forEach(input=>input.addEventListener("change",()=>{$("profile-description").textContent=descriptions[profile()];}));
$("reload-latest").addEventListener("click",loadLatest);
$("apply-protection").addEventListener("click",applyProtection);
loadLatest();
})();