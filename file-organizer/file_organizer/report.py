"""Numbers behind the dashboard and the HTML report, computed with SQL so they stay fast at 500,000 files."""
from __future__ import annotations

import json
import os
import time

from .common import human_size
from .config import MEDIA_EXTS, VIDEO_EXTS

TOP_DESTINATIONS = ("Projects", "Photos", "Documents", "Graphics-Visuals", "Videos", "Archives", "Other",
                    "_Duplicates", "Executables-System")
DEST_LABELS = {"Projects": "Project folders", "Photos": "Photos & trips", "Documents": "Documents by type",
               "Graphics-Visuals": "Graphics", "Videos": "Videos", "Archives": "Archives", "Other": "Other files",
               "_Duplicates": "Duplicates for review", "Executables-System": "Executables", "Left in place": "Left in place"}


def latest_dry_run(conn) -> int | None:
    row = conn.execute("SELECT id FROM runs WHERE kind='dryrun' AND status='complete' "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def build_report(conn, run_id: int) -> dict:
    source, dest, options, started, finished = conn.execute(
        "SELECT source, dest, options, started, finished FROM runs WHERE id=?", (run_id,)).fetchone()
    files, total_bytes = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE run_id=?",
                                      (run_id,)).fetchone()
    plan = {s: (n, b) for s, n, b in conn.execute(
        "SELECT status, COUNT(*), COALESCE(SUM(size),0) FROM plan WHERE run_id=? GROUP BY status", (run_id,))}

    # where the files will end up: first folder under the destination
    start = len(os.path.abspath(dest)) + 2
    dest_rows = conn.execute(
        "SELECT CASE WHEN dst IS NULL THEN 'Left in place' ELSE "
        "substr(replace(substr(dst, ?), char(92), '/'), 1, instr(replace(substr(dst, ?), char(92), '/') || '/', '/') - 1) END, "
        "COUNT(*), COALESCE(SUM(size),0) FROM plan WHERE run_id=? GROUP BY 1 ORDER BY 3 DESC",
        (start, start, run_id)).fetchall()
    destinations = [{"key": k, "label": DEST_LABELS.get(k, k), "files": n, "bytes": b} for k, n, b in dest_rows]

    categories = [{"name": c, "files": n, "bytes": b} for c, n, b in conn.execute(
        "SELECT category, COUNT(*), COALESCE(SUM(size),0) FROM files WHERE run_id=? GROUP BY category "
        "ORDER BY 3 DESC", (run_id,))]

    project_rows = conn.execute(
        "SELECT project, COUNT(*), COALESCE(SUM(size),0) FROM files WHERE run_id=? AND project IS NOT NULL "
        "GROUP BY project ORDER BY 2 DESC", (run_id,)).fetchall()
    roles_by_project: dict[str, dict[str, int]] = {}
    for proj, role, n in conn.execute(
            "SELECT project, COALESCE(role, subgroup, 'Other'), COUNT(*) FROM files WHERE run_id=? "
            "AND project IS NOT NULL GROUP BY 1, 2", (run_id,)):
        roles_by_project.setdefault(proj, {})[role] = n
    projects = [{"label": p, "files": n, "bytes": b, "roles": roles_by_project.get(p, {})}
                for p, n, b in project_rows]

    trips = [{"label": t, "files": n, "start": a, "end": z} for t, n, a, z in conn.execute(
        "SELECT trip, COUNT(*), MIN(taken), MAX(taken) FROM files WHERE run_id=? AND trip IS NOT NULL "
        "GROUP BY trip ORDER BY 2 DESC", (run_id,))]
    vids = ",".join(f"'{e}'" for e in sorted(VIDEO_EXTS))
    months = [{"month": ym, "photos": n - v, "videos": v} for ym, n, v in conn.execute(
        f"SELECT strftime('%Y-%m', taken, 'unixepoch'), COUNT(*), SUM(CASE WHEN ext IN ({vids}) THEN 1 ELSE 0 END) "
        "FROM files WHERE run_id=? AND taken IS NOT NULL GROUP BY 1 ORDER BY 1", (run_id,))]
    countries = [{"code": c, "files": n} for c, n in conn.execute(
        "SELECT country, COUNT(*) FROM files WHERE run_id=? AND lat IS NOT NULL AND country IS NOT NULL "
        "GROUP BY 1 ORDER BY 2 DESC", (run_id,))]
    roles = [{"role": r, "files": n} for r, n in conn.execute(
        "SELECT role, COUNT(*) FROM files WHERE run_id=? AND project IS NOT NULL AND role IS NOT NULL "
        "GROUP BY 1 ORDER BY 2 DESC", (run_id,))]
    dup_n, dup_b = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE run_id=? AND is_dup=1",
                                (run_id,)).fetchone()
    dup_groups = conn.execute("SELECT COUNT(DISTINCT hash) FROM files WHERE run_id=? AND is_dup=1",
                              (run_id,)).fetchone()[0]
    flagged_n, flagged_b = conn.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE run_id=? AND cleanup=1",
                                        (run_id,)).fetchone()
    media_n = conn.execute("SELECT COUNT(*) FROM files WHERE run_id=? AND taken IS NOT NULL", (run_id,)).fetchone()[0]
    in_projects = sum(p["files"] for p in projects)
    opts = json.loads(options or "{}")

    data = {
        "run_id": run_id, "source": source, "dest": dest, "generated": time.strftime("%Y-%m-%d %H:%M"),
        "seconds": round((finished or started) - started, 1),
        "totals": {"files": files, "bytes": total_bytes, "planned": plan.get("planned", (0, 0))[0],
                   "planned_bytes": plan.get("planned", (0, 0))[1], "left": plan.get("skipped", (0, 0))[0],
                   "projects": len(projects), "in_projects": in_projects, "trips": len(trips), "media": media_n,
                   "duplicates": dup_n, "duplicate_bytes": dup_b, "duplicate_groups": dup_groups,
                   "flagged": flagged_n, "flagged_bytes": flagged_b},
        "destinations": destinations, "categories": categories, "projects": projects, "trips": trips,
        "months": months, "countries": countries, "roles": roles, "options": opts,
    }
    data["insights"] = _insights(data)
    return data


def _insights(d: dict) -> list[dict]:
    """Plain-language findings and tips. Each: {kind: good|tip|warn, text}."""
    t, out = d["totals"], []
    grouped = t["in_projects"] + t["media"]
    if t["files"]:
        pct = round(100 * grouped / t["files"])
        out.append({"kind": "good", "text": f"{pct}% of files ({grouped:,}) were recognised as belonging to a "
                                            f"project, trip or event; the rest are sorted by type."})
    if t["duplicates"]:
        out.append({"kind": "tip", "text": f"{t['duplicates']:,} duplicate files in {t['duplicate_groups']:,} groups "
                                           f"waste {human_size(t['duplicate_bytes'])}. They go to a review folder; nothing is deleted."})
    if d["trips"] and not d["options"].get("home_country"):
        top = d["countries"][0]["code"] if d["countries"] else "?"
        out.append({"kind": "tip", "text": f"Photos are filed as trips (e.g. {d['trips'][0]['label']}). If you live "
                                           f"there, set Home country to {top} in Settings and they become normal events."})
    for p in d["projects"]:
        idr = p["roles"].get("Identity & KYC", 0)
        if p["files"] >= 3 and idr / p["files"] >= 0.8:
            out.append({"kind": "warn", "text": f"'{p['label']}' is mostly ID and KYC papers. If that is a person's "
                                                f"name, add it to 'My name' in Settings so it is not treated as a project."})
            break
    if t["flagged"]:
        out.append({"kind": "warn", "text": f"{t['flagged']:,} executables and system files are flagged "
                                            f"({human_size(t['flagged_bytes'])}). They stay where they are."})
    if t["files"] and not d["projects"]:
        out.append({"kind": "tip", "text": "No project folders found. Projects appear when several files share a "
                                           "distinctive word or code in their names, folders or text."})
    return out


def sample_rows(conn, run_id: int, dest: str, limit: int = 20000) -> tuple[list[list], int]:
    """Plan rows for the report table: [source, destination, category, project, role, why, size, status]."""
    root = os.path.abspath(dest)
    total = conn.execute("SELECT COUNT(*) FROM plan WHERE run_id=?", (run_id,)).fetchone()[0]
    rows = []
    for src, dst, cat, proj, role, why, size, status in conn.execute(
            "SELECT p.src, p.dst, f.category, f.project, f.role, f.reason, p.size, p.status FROM plan p "
            "JOIN files f ON f.id = p.file_id WHERE p.run_id=? ORDER BY (f.project IS NULL), f.project, p.id "
            "LIMIT ?", (run_id, limit)):
        rel = os.path.relpath(dst, root) if dst else ""
        rows.append([src, rel, cat, proj or "", role or "", why or "", size, status])
    return rows, total


def write_html(conn, run_id: int, path: str, max_rows: int = 20000) -> str:
    """Self-contained report: one file, no internet, opens in any browser."""
    data = build_report(conn, run_id)
    rows, total = sample_rows(conn, run_id, data["dest"], max_rows)
    data["rows"], data["rows_total"] = rows, total
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(HTML_TEMPLATE.replace("__DATA__", blob))
    return path


HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>File Organizer Report</title>
<style>
:root{color-scheme:dark;--bg:#0b0e17;--card:#131827;--card2:#182036;--line:#243049;--tx:#e8ecf6;--mu:#8a96b2;--a1:#6ee7d8;--a2:#7c8cff;--a3:#f472b6;--a4:#fbbf24;--a5:#34d399;--a6:#60a5fa}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(1200px 600px at 10% -10%,#1b2450 0,transparent 60%) fixed,var(--bg);color:var(--tx);font:14px/1.5 "Segoe UI Variable","Segoe UI",system-ui,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:26px;margin:0;letter-spacing:-.02em}h2{font-size:13px;margin:0 0 14px;color:var(--mu);font-weight:600;letter-spacing:.08em;text-transform:uppercase}
.sub{color:var(--mu);margin:4px 0 24px;word-break:break-all}
.grid{display:grid;gap:16px}.kpis{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))}.two{grid-template-columns:repeat(auto-fit,minmax(380px,1fr))}
.card{background:linear-gradient(180deg,var(--card2),var(--card));border:1px solid var(--line);border-radius:16px;padding:18px}
.kpi b{display:block;font-size:30px;letter-spacing:-.02em;line-height:1.1;background:linear-gradient(90deg,var(--a1),var(--a2));-webkit-background-clip:text;background-clip:text;color:transparent}
.kpi span{color:var(--mu);font-size:12px}.kpi small{display:block;color:var(--mu);margin-top:2px}
.sec{margin-top:16px}.leg{display:flex;flex-wrap:wrap;gap:6px 14px;margin-top:10px;font-size:12px;color:var(--mu)}
.leg i{display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:6px}
.donut{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.bar{display:grid;grid-template-columns:minmax(90px,170px) 1fr 70px;gap:10px;align-items:center;margin:7px 0;font-size:13px}
.bar .t{height:9px;border-radius:6px;background:var(--line);overflow:hidden}.bar .t div{height:100%;border-radius:6px;background:linear-gradient(90deg,var(--a2),var(--a1))}
.bar .l{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.bar .n{text-align:right;color:var(--mu);font-variant-numeric:tabular-nums}
.ins{display:flex;gap:10px;padding:10px 12px;border-radius:12px;background:var(--card2);border:1px solid var(--line);margin:8px 0}
.ins em{font-style:normal;width:8px;border-radius:4px;flex:none}.good em{background:var(--a5)}.tip em{background:var(--a6)}.warn em{background:var(--a4)}
.tools{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
input,select{background:var(--card2);color:var(--tx);border:1px solid var(--line);border-radius:10px;padding:8px 12px;font:inherit}input{flex:1;min-width:200px}
table{width:100%;border-collapse:collapse;font-size:12.5px}th{text-align:left;color:var(--mu);font-weight:600;padding:8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--card)}
td{padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top;word-break:break-all}td.s{white-space:nowrap;text-align:right;color:var(--mu)}
.chip{display:inline-block;padding:1px 8px;border-radius:99px;background:var(--line);font-size:11px;white-space:nowrap}
.scroll{max-height:560px;overflow:auto;border-radius:10px}.pg{display:flex;gap:10px;align-items:center;justify-content:flex-end;margin-top:10px;color:var(--mu)}
button{background:var(--card2);color:var(--tx);border:1px solid var(--line);border-radius:10px;padding:6px 14px;cursor:pointer;font:inherit}button:disabled{opacity:.4;cursor:default}
.empty{color:var(--mu);padding:8px 0}
</style></head><body><div class="wrap">
<h1>Sorting preview</h1><div class="sub" id="sub"></div>
<div class="grid kpis" id="kpis"></div>
<div class="grid two sec">
 <div class="card"><h2>Where files will go</h2><div class="donut" id="donut"></div></div>
 <div class="card"><h2>What we found</h2><div id="ins"></div></div>
</div>
<div class="grid two sec">
 <div class="card"><h2>Projects</h2><div id="proj"></div></div>
 <div class="card"><h2>Photo trips</h2><div id="trips"></div></div>
</div>
<div class="card sec"><h2>Photos and videos over time</h2><div id="time"></div></div>
<div class="card sec"><h2>Every file</h2>
 <div class="tools"><input id="q" placeholder="Search path, project, role..."><select id="fp"><option value="">All projects</option></select><select id="fc"><option value="">All types</option></select></div>
 <div class="scroll"><table><thead><tr><th>Source</th><th>New location</th><th>Project</th><th>Role</th><th style="text-align:right">Size</th></tr></thead><tbody id="tb"></tbody></table></div>
 <div class="pg"><span id="pn"></span><button id="pp">Prev</button><button id="pxt">Next</button></div>
 <div class="empty" id="cap"></div></div>
</div>
<script id="d" type="application/json">__DATA__</script>
<script>
(function(){
const D=JSON.parse(document.getElementById('d').textContent);
const C=['#6ee7d8','#7c8cff','#f472b6','#fbbf24','#34d399','#60a5fa','#fb923c','#a78bfa','#94a3b8'];
const $=id=>document.getElementById(id), n=x=>x.toLocaleString();
const sz=b=>{const u=['B','KB','MB','GB','TB'];let i=0;while(b>=1024&&i<4){b/=1024;i++}return (i?b.toFixed(b<10?1:0):b)+' '+u[i]};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const T=D.totals;
$('sub').textContent=D.source+'  →  '+D.dest+'   ·   '+D.generated+'   ·   nothing has been changed yet';
$('kpis').innerHTML=[['Files scanned',n(T.files),sz(T.bytes)],['Project folders',n(T.projects),n(T.in_projects)+' files grouped'],['Photo trips',n(T.trips),n(T.media)+' dated photos & videos'],['Duplicates',n(T.duplicates),sz(T.duplicate_bytes)+' reclaimable'],['Left in place',n(T.left),n(T.flagged)+' flagged']]
 .map(k=>'<div class="card kpi"><span>'+k[0]+'</span><b>'+k[1]+'</b><small>'+k[2]+'</small></div>').join('');
// donut
(function(){const s=D.destinations,tot=s.reduce((a,b)=>a+b.files,0)||1,R=70,r=46,cx=80;let a0=-Math.PI/2,p='';
 s.forEach((d,i)=>{const a=d.files/tot*2*Math.PI,a1=a0+Math.min(a,6.2830),big=a1-a0>Math.PI?1:0;
  const pt=(ang,rad)=>[cx+rad*Math.cos(ang),cx+rad*Math.sin(ang)];const [x0,y0]=pt(a0,R),[x1,y1]=pt(a1,R),[x2,y2]=pt(a1,r),[x3,y3]=pt(a0,r);
  p+='<path d="M'+x0+' '+y0+'A'+R+' '+R+' 0 '+big+' 1 '+x1+' '+y1+'L'+x2+' '+y2+'A'+r+' '+r+' 0 '+big+' 0 '+x3+' '+y3+'Z" fill="'+C[i%C.length]+'"><title>'+esc(d.label)+': '+n(d.files)+'</title></path>';a0+=a});
 $('donut').innerHTML='<svg width="160" height="160" viewBox="0 0 160 160">'+p+'<text x="80" y="78" text-anchor="middle" fill="currentColor" font-size="20" font-weight="700">'+n(tot)+'</text><text x="80" y="96" text-anchor="middle" fill="#8a96b2" font-size="10">files</text></svg><div class="leg" style="flex-direction:column;margin:0">'+s.map((d,i)=>'<div><i style="background:'+C[i%C.length]+'"></i>'+esc(d.label)+' <b style="color:var(--tx)">'+n(d.files)+'</b> · '+sz(d.bytes)+'</div>').join('')+'</div>'})();
$('ins').innerHTML=D.insights.map(i=>'<div class="ins '+i.kind+'"><em></em><div>'+esc(i.text)+'</div></div>').join('')||'<div class="empty">Nothing to flag.</div>';
function bars(el,items,lab,val,note){const m=Math.max(1,...items.map(val));$(el).innerHTML=items.length?items.slice(0,12).map(i=>'<div class="bar"><div class="l" title="'+esc(lab(i))+'">'+esc(lab(i))+'</div><div class="t"><div style="width:'+(val(i)/m*100)+'%"></div></div><div class="n">'+n(val(i))+'</div></div>'+(note?note(i):'')).join(''):'<div class="empty">None found.</div>'}
bars('proj',D.projects,p=>p.label,p=>p.files,p=>'<div class="leg" style="margin:-2px 0 6px">'+Object.entries(p.roles).sort((a,b)=>b[1]-a[1]).slice(0,5).map(([k,v])=>'<span class="chip">'+esc(k)+' '+v+'</span>').join('')+'</div>');
bars('trips',D.trips,t=>t.label,t=>t.files,t=>'<div class="leg" style="margin:-2px 0 6px">'+new Date(t.start*1000).toISOString().slice(0,10)+' to '+new Date(t.end*1000).toISOString().slice(0,10)+'</div>');
(function(){const m=D.months;if(!m.length){$('time').innerHTML='<div class="empty">No dated photos or videos.</div>';return}
 const W=1100,H=190,pl=36,bw=Math.max(3,Math.min(40,(W-pl)/m.length-3)),mx=Math.max(...m.map(x=>x.photos+x.videos));let s='';
 m.forEach((x,i)=>{const X=pl+i*(bw+3),h1=x.photos/mx*(H-40),h2=x.videos/mx*(H-40);
  s+='<rect x="'+X+'" y="'+(H-24-h1)+'" width="'+bw+'" height="'+h1+'" rx="2" fill="#7c8cff"><title>'+x.month+': '+x.photos+' photos</title></rect><rect x="'+X+'" y="'+(H-24-h1-h2)+'" width="'+bw+'" height="'+h2+'" rx="2" fill="#6ee7d8"><title>'+x.month+': '+x.videos+' videos</title></rect>';
  if(i%Math.ceil(m.length/12)===0)s+='<text x="'+X+'" y="'+(H-8)+'" fill="#8a96b2" font-size="10">'+x.month+'</text>'});
 $('time').innerHTML='<svg viewBox="0 0 '+W+' '+H+'" width="100%" height="190"><text x="0" y="12" fill="#8a96b2" font-size="10">'+n(mx)+'</text>'+s+'</svg><div class="leg"><span><i style="background:#7c8cff"></i>Photos</span><span><i style="background:#6ee7d8"></i>Videos</span></div>'})();
// table
const R=D.rows,PS=100;let page=0,flt=R;
[...new Set(R.map(r=>r[3]).filter(Boolean))].sort().forEach(p=>$('fp').add(new Option(p,p)));
[...new Set(R.map(r=>r[2]))].sort().forEach(p=>$('fc').add(new Option(p,p)));
function apply(){const q=$('q').value.toLowerCase(),p=$('fp').value,c=$('fc').value;
 flt=R.filter(r=>(!p||r[3]===p)&&(!c||r[2]===c)&&(!q||(r[0]+' '+r[1]+' '+r[3]+' '+r[4]+' '+r[5]).toLowerCase().includes(q)));page=0;draw()}
function draw(){const s=flt.slice(page*PS,page*PS+PS);
 $('tb').innerHTML=s.map(r=>'<tr title="'+esc(r[5])+'"><td>'+esc(r[0])+'</td><td>'+esc(r[1]||'(stays here)')+'</td><td>'+(r[3]?'<span class="chip">'+esc(r[3])+'</span>':'')+'</td><td>'+esc(r[4])+'</td><td class="s">'+sz(r[6])+'</td></tr>').join('');
 $('pn').textContent=n(flt.length?page*PS+1:0)+' to '+n(Math.min(flt.length,page*PS+PS))+' of '+n(flt.length);
 $('pp').disabled=page===0;$('pxt').disabled=(page+1)*PS>=flt.length}
$('pp').onclick=()=>{page--;draw()};$('pxt').onclick=()=>{page++;draw()};
['q','fp','fc'].forEach(i=>$(i).addEventListener('input',apply));
$('cap').textContent=D.rows_total>R.length?'Showing the first '+n(R.length)+' of '+n(D.rows_total)+' files. The CSV next to this report has all of them.':'';
draw();
})();
</script></body></html>
"""


def _rel(path: str, base: str) -> str:
    try:
        return os.path.relpath(path, base)
    except ValueError:      # different drive on Windows
        return path


def query_rows(conn, run_id: int, dest: str, text: str = "", project: str = "", limit: int = 200,
               offset: int = 0) -> tuple[list[tuple], int]:
    """One page of plan rows for the Review table, with optional text and project filters."""
    where, args = ["p.run_id=?"], [run_id]
    if project == "(no project)":
        where.append("f.project IS NULL")
    elif project:
        where.append("f.project=?")
        args.append(project)
    for word in text.split():
        where.append("(p.src LIKE ? OR p.dst LIKE ? OR f.project LIKE ? OR f.role LIKE ?)")
        args += [f"%{word}%"] * 4
    clause = " AND ".join(where)
    frm = "FROM plan p JOIN files f ON f.id = p.file_id"
    total = conn.execute(f"SELECT COUNT(*) {frm} WHERE {clause}", args).fetchone()[0]
    root = os.path.abspath(dest)
    source = conn.execute("SELECT source FROM runs WHERE id=?", (run_id,)).fetchone()[0]
    out = []
    for src, dst, proj, role, size in conn.execute(
            f"SELECT p.src, p.dst, f.project, f.role, p.size {frm} WHERE {clause} ORDER BY p.id LIMIT ? OFFSET ?",
            args + [limit, offset]):
        out.append((_rel(src, source), os.path.relpath(dst, root) if dst else "(stays here)", proj or "", role or "",
                    size))
    return out, total


def project_names(conn, run_id: int) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT project FROM files WHERE run_id=? AND project IS NOT NULL GROUP BY project ORDER BY COUNT(*) DESC",
        (run_id,))]


def write_audit(conn, exec_id: int, path: str) -> str:
    """Every action of one execution (what, from, to, result, when) as a CSV."""
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "action", "status", "size_bytes", "source", "destination", "error"])
        for ts, op, st, size, src, dst, err in conn.execute(
                "SELECT ts, op, status, size, src, dst, error FROM journal WHERE exec_id=? ORDER BY id", (exec_id,)):
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "", op, st, size, src,
                        dst, err or ""])
    return path


def fingerprint(folder: str) -> tuple[int, float]:
    """(file count, newest modified time) of a folder tree. Cheap change detector for the watch feature."""
    count, newest = 0, 0.0
    stack = [folder]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            count += 1
                            newest = max(newest, e.stat().st_mtime)
                    except OSError:
                        continue
        except OSError:
            continue
    return count, newest
