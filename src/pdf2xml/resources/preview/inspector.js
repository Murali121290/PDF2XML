"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const hash = new URLSearchParams(location.hash.slice(1));
const S = {docs:[], doc:"", file:"", page:Number(hash.get("page")) || 0, data:null, lines:[],
  sourceKey:"", selected:null, scope:"page", range:[1,1], scale:1, fit:"page", sequence:0,
  filters:new Set(), filtersReady:false, searchLine:-1, busy:false, stamp:null};
const ROW = 20;
const names = {heading:"Heading", paragraph:"Paragraph", caption:"Caption", list:"List container",
  list_item:"List item", table:"Table", figure:"Figure", formula:"Formula", box:"Box", code:"Code", artifact:"Artifact"};
async function request(name, params={}, options={}) {
  const response = await fetch(`/api/${name}?${new URLSearchParams(params)}`, options);
  if (!response.ok) {
    const text = await response.text();
    try {const data=JSON.parse(text); throw new Error(data.message || data.detail || text);} catch(error) {
      if (error instanceof SyntaxError) throw new Error(text); throw error;
    }
  }
  return name === "source" ? response.text() : response.json();
}
function notice(message) {$("notice").textContent=message || ""; $("notice").hidden=!message;}
function setBusy(busy) {
  S.busy=busy; document.body.classList.toggle("loading",busy);
  $("previous").disabled=busy || !S.data || S.data.pages.indexOf(S.page)<=0;
  $("next").disabled=busy || !S.data || S.data.pages.indexOf(S.page)>=S.data.pages.length-1;
}
async function init() {
  try {
    const result=await request("docs"); S.docs=result.docs;
    $("document").innerHTML=S.docs.map(d=>`<option value="${esc(d.name)}">${esc(d.name)}</option>`).join("");
    if (!S.docs.length) {
      $("pdf-empty").innerHTML="<strong>No converted documents yet</strong><p>Use Upload & convert to create your first layout preview.</p>";
      $("source-status").textContent="No document"; return;
    }
    S.doc=S.docs.some(d=>d.name===hash.get("doc")) ? hash.get("doc") : S.docs[0].name;
    $("document").value=S.doc; fillFiles(hash.get("file")); await loadPage();
  } catch(error) {notice(error.message); $("source-status").textContent="Could not open document";}
}
function fillFiles(wanted) {
  const files=S.docs.find(d=>d.name===S.doc).files;
  $("xml-file").innerHTML=files.map(f=>`<option value="${esc(f)}">${esc(f)}</option>`).join("");
  S.file=files.includes(wanted) ? wanted : files.includes("30_canonical.xml") ? "30_canonical.xml" : files[0];
  $("xml-file").value=S.file; S.selected=null;
  $("download").href=`/api/source?${new URLSearchParams({doc:S.doc,file:S.file})}`;
  $("download").download=S.file;
  $("proof-link").href=`/#${new URLSearchParams({doc:S.doc,file:S.file})}`;
}
async function loadPage(force=false) {
  const sequence=++S.sequence;
  if (S.abort) S.abort.abort(); S.abort=new AbortController();
  setBusy(true); notice(""); $("status-text").textContent="Loading PDF page and XML…";
  const key=`${S.doc}:${S.file}`;
  try {
    const [data,source]=await Promise.all([
      request("inspection",{doc:S.doc,file:S.file,page:S.page},{signal:S.abort.signal}),
      key!==S.sourceKey || force ? request("source",{doc:S.doc,file:S.file},{signal:S.abort.signal}) : Promise.resolve(null)
    ]);
    if (sequence!==S.sequence) return;
    S.data=data; S.page=data.page.number; S.stamp=data.stamp;
    if (source!==null) {S.lines=source.split(/\r?\n/); S.sourceKey=key; S.searchLine=-1;}
    if (!data.blocks.some(b=>b.id===S.selected)) S.selected=null;
    $("page").value=S.page; $("page").max=Math.max(...data.pages);
    $("page-total").textContent=`of ${Math.max(...data.pages)}`;
    $("page").title=data.pages.length===Math.max(...data.pages) ? "Physical PDF page number" : `Converted pages: ${data.pages.join(", ")}`;
    $("source-status").textContent=data.source_verified ? "Source matched" : "Original PDF needed";
    $("source-status").className=`status ${data.source_verified ? "verified" : "needed"}`;
    $("attach-label").hidden=data.source_verified;
    const quality=data.quality || {};
    $("checks").textContent=quality.status ? `${quality.status==="passed" ? "Quality Report: passed" : "Quality Report: review"} · ${quality.issue_count || 0}` : "Quality Report";
    $("checks").classList.toggle("ok",quality.status==="passed");
    $("block-count").textContent=`${data.blocks.filter(b=>b.order!==null).length} reading blocks`;
    $("mapping-note").textContent=data.mapping_note;
    $("paper").hidden=true; $("pdf-empty").hidden=data.source_available;
    if (data.source_available) {
      $("pdf-image").src=`/api/page-image?${new URLSearchParams({doc:S.doc,page:S.page,v:data.stamp})}`;
      $("pdf-image").alt=`Original PDF page ${S.page}`;
      try {await $("pdf-image").decode();} catch {throw new Error("The PDF page image could not be loaded. Try Refresh.");}
      if (sequence!==S.sequence) return;
      $("paper").hidden=false; sizePaper();
    } else {
      $("pdf-empty").innerHTML="<strong>Attach the original PDF</strong><p>Choose the same PDF used for this conversion. Its fingerprint will be checked before any boxes are shown.</p>";
    }
    drawLegend(); drawOverlay(); drawSelection(); drawOrder(); setXmlRange();
    $("status-text").textContent=data.reading_order_note;
    history.replaceState(null,"",`#${new URLSearchParams({doc:S.doc,file:S.file,page:S.page})}`);
  } catch(error) {
    if (error.name!=="AbortError" && sequence===S.sequence) {notice(error.message); $("status-text").textContent="Preview could not be loaded.";}
  } finally {if (sequence===S.sequence) setBusy(false);}
}
function drawLegend() {
  const counts={}; for (const b of [...S.data.blocks,...S.data.artifacts]) counts[b.type]=(counts[b.type] || 0)+1;
  if (!S.filtersReady) {for (const kind of Object.keys(S.data.palette)) if (!["artifact","list"].includes(kind)) S.filters.add(kind); S.filtersReady=true;}
  $("legend").innerHTML=Object.entries(S.data.palette).filter(([kind])=>counts[kind]).map(([kind,color])=>
    `<label style="--c:${color}" title="${esc(names[kind])}: ${color}"><input type="checkbox" data-kind="${kind}" ${S.filters.has(kind) ? "checked" : ""}><span>${esc(names[kind])}</span><span class="count">${counts[kind]}</span></label>`).join("");
}
function records() {return S.data ? [...S.data.blocks,...S.data.artifacts] : [];}
function visible(b) {return S.filters.has(b.type) && (!b.decorative || $("show-decoration").checked);}
function drawOverlay() {
  if (!S.data) return;
  const {width,height,origin}=S.data.page;
  $("overlay").setAttribute("viewBox",`0 0 ${width} ${height}`);
  const chosen=records().filter(b=>visible(b) || b.id===S.selected);
  const readable=chosen.filter(b=>b.order!==null).sort((a,b)=>a.order-b.order);
  const point=b=>[b.bbox[0]-origin[0]+7,Math.max(7,b.bbox[1]-origin[1]-7)];
  let content=`<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="#173d48"/></marker></defs>`;
  if ($("show-flow").checked) content+=readable.slice(1).map((b,i)=>{
    const a=point(readable[i]), c=point(b); return `<path class="flow" d="M${a[0]},${a[1]} L${c[0]},${c[1]}" marker-end="url(#arrow)"/>`;
  }).join("");
  const sorted=[...chosen.filter(b=>b.container),...chosen.filter(b=>!b.container)];
  content+=sorted.map(b=>{
    const [x0,y0,x1,y1]=b.bbox, x=x0-origin[0],y=y0-origin[1],selected=b.id===S.selected;
    const color=S.data.palette[b.type], p=point(b), number=b.order;
    return `<g class="block ${b.container ? "container" : ""} ${b.type==="artifact" ? "artifact" : ""} ${selected ? "selected" : ""}" style="--c:${color}" data-id="${esc(b.id)}" role="button" tabindex="0" aria-label="${number!==null ? `Reading order ${number}, ` : ""}${esc(names[b.type])} ${esc(b.id)}">
      <title>${esc(`${number!==null ? number+" · " : ""}${names[b.type]} · ${b.id}\n${b.text.slice(0,160)}`)}</title>
      <rect x="${x}" y="${y}" width="${Math.max(1,x1-x0)}" height="${Math.max(1,y1-y0)}" ${$("show-boxes").checked || selected ? "" : 'style="display:none"'}/>
      ${$("show-order").checked && number!==null ? `<circle class="badge" cx="${p[0]}" cy="${p[1]}" r="8"/><text x="${p[0]}" y="${p[1]+.3}">${number}</text>` : ""}</g>`;
  }).join("");
  $("overlay").innerHTML=content;
}
function fittedScale(mode, pageWidth, pageHeight, viewportWidth, viewportHeight) {
  const width=Math.max(100,viewportWidth-32)/pageWidth;
  const height=Math.max(100,viewportHeight-32)/pageHeight;
  return mode==="page" ? Math.min(width,height) : width;
}
function sizePaper() {
  if (!S.data) return;
  if (S.fit) S.scale=fittedScale(S.fit,S.data.page.width,S.data.page.height,
    $("pdf-scroll").clientWidth,$("pdf-scroll").clientHeight);
  $("paper").style.width=`${S.data.page.width*S.scale}px`;
  $("zoom-value").textContent=`${Math.round(S.scale*100)}%`;
  $("fit-page").classList.toggle("active",S.fit==="page");
  $("fit").classList.toggle("active",S.fit==="width");
}
function zoom(delta) {S.fit=false; S.scale=Math.min(3.5,Math.max(.3,S.scale+delta)); sizePaper();}
function selectBlock(id,fromXml=false) {
  const b=records().find(item=>item.id===id); if (!b) return;
  S.selected=id; drawOverlay(); drawSelection(); drawOrder();
  if (!fromXml && b.xml) gotoLine(b.xml.start);
  if (fromXml && S.data.source_available) {
    const top=(b.bbox[1]-S.data.page.origin[1])*S.scale;
    $("pdf-scroll").scrollTo({top:Math.max(0,top-$("pdf-scroll").clientHeight*.3),behavior:"smooth"});
  }
  $("match-status").classList.toggle("warning",b.link!=="exact");
  $("match-status").textContent=b.link==="exact" ? "Exact block link" : b.link==="unique-text" ? "Unique text match" : "No reliable XML link";
  $("status-text").textContent=b.xml ? `${b.id} · ${names[b.type]} · XML lines ${b.xml.start}–${b.xml.end}` : `${b.id} · No reliable XML match. Canonical XML provides exact block links.`;
  renderXml();
}
function drawSelection() {
  const b=records().find(item=>item.id===S.selected);
  $("selection-summary").textContent=b ? `${b.id} · ${names[b.type]}${b.order!==null ? ` · order ${b.order}` : ""}` : "Click a box to inspect XML";
  $("selection").innerHTML=b ? `<div class="selected-title"><span class="type-swatch" style="--c:${S.data.palette[b.type]}"></span>${esc(names[b.type])} <span class="muted">${esc(b.id)}</span>${b.order!==null ? `<span class="muted">· order ${b.order}</span>` : ""}</div><p class="selected-copy">${esc(b.text.slice(0,290) || (b.container ? "Container for nested content." : b.decorative ? "Decorative graphic." : "Image or non-text content."))}</p><div class="selected-meta">PDF bbox: ${b.bbox.map(n=>Math.round(n*10)/10).join(", ")}${b.xml ? ` · XML ${b.xml.start}–${b.xml.end}` : ""}</div>` :
    `<p class="muted">Click a colored box to inspect its XML.</p><p class="selected-copy">Numbers follow recovered canonical reading order. Dashed outlines are containers; artifacts and decorations are not numbered.</p>`;
  if (!b) {$("match-status").textContent=""; $("match-status").classList.remove("warning");}
}
function drawOrder() {
  $("order-list").innerHTML=S.data.blocks.filter(b=>b.order!==null).map(b=>`<button data-id="${esc(b.id)}" class="${b.id===S.selected ? "selected" : ""}" style="--c:${S.data.palette[b.type]}"><span class="number">${b.order}</span><span>${esc(names[b.type])}</span><span class="excerpt">${esc(b.text || b.id)}</span></button>`).join("");
}
function setXmlRange() {
  if (!S.data) return;
  let start=1,end=Math.max(1,S.lines.length);
  const spans=S.data.blocks.filter(b=>b.xml).map(b=>b.xml);
  if (S.scope==="page" && spans.length) {start=Math.max(1,Math.min(...spans.map(s=>s.start))-1); end=Math.min(end,Math.max(...spans.map(s=>s.end))+1);}
  S.range=[start,end]; $("xml-lines").style.height=`${(end-start+1)*ROW}px`;
  $("xml-context").textContent=`${S.scope==="page" && spans.length ? `Page ${S.page} · ` : ""}Lines ${start.toLocaleString()}–${end.toLocaleString()} of ${S.lines.length.toLocaleString()}`;
  $("xml-scroll").scrollTop=0; renderXml();
}
function highlight(line) {
  return esc(line).replace(/(&lt;\/?)([\w:-]+)(.*?)(\/?&gt;)/g,(_,a,tag,attributes,end)=>
    `<span class="tag">${a}${tag}</span>${attributes.replace(/([\w:-]+)=(&quot;.*?&quot;)/g,'<span class="attribute">$1</span>=<span class="value">$2</span>')}<span class="tag">${end}</span>`);
}
function renderXml() {
  if (!S.data) return;
  const viewport=$("xml-scroll"), [start,end]=S.range;
  const first=Math.max(start,start+Math.floor(viewport.scrollTop/ROW)-12);
  const last=Math.min(end,first+Math.ceil(viewport.clientHeight/ROW)+30);
  const selected=records().find(b=>b.id===S.selected)?.xml;
  let html="";
  for (let line=first;line<=last;line++) html+=`<div class="xml-row ${selected && line>=selected.start && line<=selected.end ? "picked" : ""} ${S.searchLine===line ? "hit" : ""}" style="top:${(line-start)*ROW}px" data-line="${line}"><span class="line-number">${line}</span><code>${highlight(S.lines[line-1] || "")}</code></div>`;
  $("xml-lines").innerHTML=html;
}
function gotoLine(line) {
  if (line<S.range[0] || line>S.range[1]) {S.scope="full"; scopeButtons(); setXmlRange();}
  $("xml-scroll").scrollTop=Math.max(0,(line-S.range[0])*ROW-80); renderXml();
}
function scopeButtons() {$("scope-page").classList.toggle("active",S.scope==="page"); $("scope-full").classList.toggle("active",S.scope==="full");}
function findNext() {
  const query=$("search").value.toLocaleLowerCase(); if (!query || !S.lines.length) return;
  const from=Math.max(0,S.searchLine);
  for (let step=0;step<S.lines.length;step++) {const i=(from+step)%S.lines.length;
    if (S.lines[i].toLocaleLowerCase().includes(query)) {S.searchLine=i+1; gotoLine(i+1); $("status-text").textContent=`Found “${$("search").value}” on XML line ${i+1}`; return;}}
  $("status-text").textContent=`No XML matches for “${$("search").value}”.`;
}
function changePage(offset) {
  if (!S.data || S.busy) return;
  const index=S.data.pages.indexOf(S.page)+offset;
  if (index>=0 && index<S.data.pages.length) {S.page=S.data.pages[index]; loadPage();}
}
$("document").onchange=()=>{S.doc=$("document").value; S.page=0; S.data=null; S.selected=null; fillFiles(); loadPage();};
$("xml-file").onchange=()=>{fillFiles($("xml-file").value); loadPage(true);};
$("previous").onclick=()=>changePage(-1); $("next").onclick=()=>changePage(1);
$("page").onchange=()=>{const page=Number($("page").value); if (S.data?.pages.includes(page)) {S.page=page; loadPage();} else {$("page").value=S.page; notice("Choose a PDF page included in this conversion.");}};
$("refresh").onclick=()=>S.doc ? loadPage(true) : init();
for (const id of ["show-boxes","show-order","show-flow","show-decoration"]) $(id).onchange=drawOverlay;
$("legend").onchange=event=>{const kind=event.target.dataset.kind; if (kind) {event.target.checked ? S.filters.add(kind) : S.filters.delete(kind); drawOverlay();}};
$("overlay").onclick=event=>{const block=event.target.closest("[data-id]"); if (block) selectBlock(block.dataset.id);};
$("overlay").onkeydown=event=>{if (["Enter"," "].includes(event.key)) {event.preventDefault(); const block=event.target.closest("[data-id]"); if (block) selectBlock(block.dataset.id);}};
$("order-list").onclick=event=>{const block=event.target.closest("[data-id]"); if (block) {selectBlock(block.dataset.id); const b=records().find(b=>b.id===block.dataset.id); $("pdf-scroll").scrollTo({top:Math.max(0,(b.bbox[1]-S.data.page.origin[1])*S.scale-80),behavior:"smooth"});}};
function details(tab) {
  const open=tab!==null;
  $("details-content").hidden=!open; $("details-close").hidden=!open;
  $("details-panel").classList.toggle("open",open);
  $("selection").hidden=tab==="order"; $("order-list").hidden=tab!=="order";
  $("selection-tab").classList.toggle("active",tab==="selection");
  $("order-tab").classList.toggle("active",tab==="order");
  $("selection-tab").setAttribute("aria-expanded",String(tab==="selection"));
  $("order-tab").setAttribute("aria-expanded",String(tab==="order"));
}
$("selection-tab").onclick=()=>details($("selection-tab").getAttribute("aria-expanded")==="true" ? null : "selection");
$("order-tab").onclick=()=>details($("order-tab").getAttribute("aria-expanded")==="true" ? null : "order");
$("details-close").onclick=()=>details(null);
$("zoom-in").onclick=()=>zoom(.15); $("zoom-out").onclick=()=>zoom(-.15);
$("fit").onclick=()=>{S.fit="width";sizePaper();};
$("fit-page").onclick=()=>{S.fit="page";sizePaper(); $("pdf-scroll").scrollTo({top:0,left:0});};
$("legend-toggle").onclick=()=>{const open=$("legend").hidden; $("legend").hidden=!open; $("legend-toggle").setAttribute("aria-expanded",String(open));};
$("focus-workspace").onclick=()=>{
  const on=document.body.classList.toggle("focus-workspace");
  $("focus-workspace").textContent=on ? "Exit focus" : "Focus";
  $("focus-workspace").setAttribute("aria-pressed",String(on));
};
document.addEventListener("click",event=>{
  if (!$("legend").hidden && !event.target.closest("#legend, #legend-toggle")) {
    $("legend").hidden=true; $("legend-toggle").setAttribute("aria-expanded","false");
  }
});
document.addEventListener("keydown",event=>{
  if (event.key!=="Escape" || $("check-dialog").open) return;
  if (!$("details-content").hidden) details(null);
  else if (!$("legend").hidden) {$("legend").hidden=true; $("legend-toggle").setAttribute("aria-expanded","false");}
  else if (document.body.classList.contains("focus-workspace")) $("focus-workspace").click();
});
$("xml-scroll").onscroll=()=>requestAnimationFrame(renderXml);
$("xml-lines").onclick=event=>{
  const row=event.target.closest("[data-line]"); if (!row) return;
  const line=Number(row.dataset.line);
  const candidates=S.data.blocks.filter(b=>b.xml && b.xml.start<=line && b.xml.end>=line).sort((a,b)=>(a.xml.end-a.xml.start)-(b.xml.end-b.xml.start));
  if (candidates.length) selectBlock(candidates[0].id,true);
};
$("scope-page").onclick=()=>{S.scope="page";scopeButtons();setXmlRange();};
$("scope-full").onclick=()=>{S.scope="full";scopeButtons();setXmlRange();};
$("search").onkeydown=event=>{if (event.key==="Enter") findNext();}; $("search").oninput=()=>{S.searchLine=-1;}; $("find-next").onclick=findNext;
$("attach").onchange=async()=>{
  const file=$("attach").files[0]; if (!file || !S.doc) return;
  try {notice("Checking the original PDF…"); await request("inspection-source",{doc:S.doc},{method:"POST",headers:{"Content-Type":"application/pdf"},body:file}); await loadPage(true);}
  catch(error) {notice(error.message);} finally {$("attach").value="";}
};
$("checks").onclick=async()=>{
  $("check-content").textContent="Loading the full report?";
  $("check-dialog").showModal();
  try {
    const report=await request("report",{doc:S.doc});
    $("check-content").innerHTML=window.renderQualityReport(report);
    const link=document.createElement("a"); link.href=`/files/${encodeURIComponent(S.doc)}/quality_report.html`;
    link.textContent="Open printable report"; link.target="_blank";
    $("check-content").prepend(link);
  } catch(error) { $("check-content").textContent=`Report unavailable: ${error.message}`; }
};
$("close-checks").onclick=()=>$("check-dialog").close();
$("live").onchange=()=>$("live-dot").classList.toggle("off",!$("live").checked);
let resizing=false;
$("splitter").onpointerdown=event=>{resizing=true; $("splitter").setPointerCapture(event.pointerId);};
$("splitter").onpointermove=event=>{if (resizing) {document.documentElement.style.setProperty("--pdf-width",`${Math.min(72,Math.max(30,event.clientX/innerWidth*100))}%`);sizePaper();}};
$("splitter").onpointerup=()=>{resizing=false;};
$("splitter").onkeydown=event=>{if (["ArrowLeft","ArrowRight"].includes(event.key)) {event.preventDefault(); const width=parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--pdf-width")) || 54; document.documentElement.style.setProperty("--pdf-width",`${Math.max(30,Math.min(72,width+(event.key==="ArrowRight" ? 2 : -2)))}%`);sizePaper();}};
new ResizeObserver(()=>{sizePaper();renderXml();}).observe($("workspace"));
new ResizeObserver(sizePaper).observe($("pdf-scroll"));
setInterval(async()=>{if (!S.doc || S.busy || !$("live").checked || document.hidden) return;
  try {const data=await request("inspection-stamp",{doc:S.doc,file:S.file}); if (data.stamp!==S.stamp) await loadPage(true);}
  catch {$("status-text").textContent="Live connection interrupted. Refresh to reconnect.";}
},1800);
init();
