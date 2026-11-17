"use strict";
window.renderQualityReport = function(report) {
  const q=report.quality || report;
  const escape=value=>String(value ?? "Not available").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const raw=value=>`<pre style="white-space:pre-wrap;overflow-wrap:anywhere;font:12px/1.5 monospace">${escape(JSON.stringify(value ?? {status:"not_run"},null,2))}</pre>`;
  const table=(headers,rows)=>`<div style="overflow:auto"><table style="border-collapse:collapse;width:100%;font-size:12px"><thead><tr>${headers.map(h=>`<th style="padding:7px;text-align:left;border-bottom:2px solid #bbc8cf">${escape(h)}</th>`).join("")}</tr></thead><tbody>${rows.map(row=>`<tr>${row.map(cell=>`<td style="padding:7px;vertical-align:top;border-bottom:1px solid #dfe5e9">${escape(typeof cell==="object" ? JSON.stringify(cell) : cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  if (!q.report_schema) return `<h3>Existing report — limited audit data</h3><p>This conversion predates the full report. Rerun conversion to collect page/font/format details.</p>${raw(report)}`;
  const statistics=q.statistics || {}, runtime=q.runtime || {};
  let html=`<h3>${escape(q.source?.original_filename || q.source?.filename)}</h3><p><strong>${escape(q.status)}</strong> · ${escape(q.errors)} errors · ${escape(q.warnings)} warnings · generated ${escape(q.generated_at)}</p>`;
  html+=`<h3>Processing and document statistics</h3>${table(["Measurement","Result"],Object.entries(statistics).map(([k,v])=>[k.replaceAll("_"," "),v]))}`;
  html+=`<h3>Processing time and traceability</h3>${raw(runtime)}<h3>Page inventory</h3>${raw(q.page_inventory)}`;
  html+=`<h3>XML validation reports</h3>`;
  for (const target of ["canonical","bits","jats","content"]) {
    const validation=q.validations?.[target] || {status:"not_run"};
    html+=`<section><h4>${escape(target.toUpperCase())} — ${escape(validation.status)}</h4>${raw(validation)}</section>`;
  }
  html+=`<h3>Character coverage</h3>${raw(q.coverage)}<h3>All findings</h3>`;
  html+=`<h3>Verification scope — including unverified checks</h3>${raw(q.verification_scope)}`;
  html+=table(["Severity","Page","Block / target","Check","Finding"],(q.issues || []).map(i=>[i.severity,i.page ?? "Document",i.block || i.target || "—",i.code,i.message]));
  html+=`<h3>Every processed page</h3>${table(["PDF page","Source words","Source characters","Output characters","Blocks","Image objects","Artifacts","Fonts","Coverage","Text status"],(q.pages || []).map(p=>[p.page,p.source_words,p.source_characters,p.output_characters,p.block_counts,p.source_image_objects,p.artifacts.length,p.fonts,p.character_coverage_ok ? "Matched" : "Mismatch",p.text_status]))}`;
  html+=`<h3>Font and Unicode audit — full details</h3>${raw(q.font_audit)}`;
  html+=`<h3>Font usage and point sizes</h3>${raw(q.font_usage)}<h3>Referenced image and formula files</h3>${raw(q.asset_files)}`;
  html+=`<h3>Canonical output font/style catalogue</h3>${raw(q.canonical_styles)}`;
  html+=`<h3>Completeness: independent text reader, images, uncovered ink and export omissions</h3>${raw(q.completeness)}`;
  html+=`<h3>Artifacts and page-specific evidence</h3>`;
  for (const page of q.pages || []) {
    if (page.artifacts.length || page.issues.length || page.font_risks?.length || Object.keys(page.completeness_evidence || {}).length || Object.keys(page.missing_characters).length || Object.keys(page.extra_characters).length)
      html+=`<h4>PDF page ${escape(page.page)}</h4>${raw({artifacts:page.artifacts,issues:page.issues,font_risks:page.font_risks,completeness_evidence:page.completeness_evidence,missing_characters:page.missing_characters,extra_characters:page.extra_characters,allowed_removed_line_hyphens:page.allowed_removed_line_hyphens})}`;
  }
  html+=`<h3>Pipeline warnings</h3>${raw(q.warnings_from_pipeline)}<h3>Methods, scope and unverified checks</h3>${raw(q.methodology)}<p>Report schema: ${escape(q.report_schema)}</p>`;
  return html;
};
