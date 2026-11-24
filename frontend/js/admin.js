// Knowledge-base admin page: upload (presigned POST straight to S3), list,
// delete and sync. The API enforces the admin group; hiding the tab is only UX.
import { request } from "./api.js";

const $ = (id) => document.getElementById(id);
const fmtSize = (n) => (n > 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1e3))} KB`);
const fmtTime = (s) => (s ? new Date(s).toLocaleString() : "");

function cell(text) { const td = document.createElement("td"); td.textContent = text ?? ""; return td; }

export async function refreshDocs(toast) {
  try {
    const { documents } = await request("GET", "/kb/documents");
    const body = $("docsBody");
    body.replaceChildren();
    if (!documents.length) {
      const tr = document.createElement("tr"); const td = cell("No documents yet."); td.colSpan = 4; tr.append(td); body.append(tr);
    }
    for (const d of documents) {
      const tr = document.createElement("tr");
      const del = document.createElement("button");
      del.className = "danger"; del.textContent = "Delete"; del.type = "button";
      del.onclick = async () => {
        if (!confirm(`Delete ${d.name}? Its vectors are removed on the next sync.`)) return;
        try { await request("DELETE", `/kb/documents/${encodeURIComponent(d.name)}`); toast(`Deleted ${d.name}`); refreshDocs(toast); }
        catch (e) { toast(e.message); }
      };
      const actions = document.createElement("td"); actions.append(del);
      tr.append(cell(d.name), cell(fmtSize(d.size)), cell(fmtTime(d.lastModified)), actions);
      body.append(tr);
    }
  } catch (e) { toast(`Could not list documents: ${e.message}`); }
}

export async function refreshJobs(toast) {
  try {
    const { jobs } = await request("GET", "/kb/sync");
    const body = $("jobsBody");
    body.replaceChildren();
    for (const j of jobs) {
      const s = j.statistics || {};
      const tr = document.createElement("tr");
      tr.append(
        cell(j.jobId), cell(j.status), cell(fmtTime(j.startedAt)),
        cell(s.numberOfDocumentsScanned), cell((s.numberOfNewDocumentsIndexed || 0) + (s.numberOfModifiedDocumentsIndexed || 0)),
        cell(s.numberOfDocumentsDeleted), cell(s.numberOfDocumentsFailed),
      );
      body.append(tr);
    }
  } catch (e) { toast(`Could not load sync status: ${e.message}`); }
}

async function uploadOne(file) {
  const { upload } = await request("POST", "/kb/documents", { filename: file.name, size: file.size });
  const form = new FormData();
  for (const [k, v] of Object.entries(upload.fields)) form.append(k, v);
  form.append("file", file); // must be the last field
  const res = await fetch(upload.url, { method: "POST", body: form });
  if (!res.ok) throw new Error(`S3 rejected ${file.name} (${res.status})`);
}

export function wireAdmin(toast) {
  $("uploadBtn").onclick = async () => {
    const files = [...$("fileInput").files];
    if (!files.length) return toast("Choose one or more files first");
    $("uploadBtn").disabled = true;
    const status = $("uploadStatus");
    for (const f of files) {
      status.textContent = `Uploading ${f.name}…`;
      try { await uploadOne(f); status.textContent = `Uploaded ${f.name}`; }
      catch (e) { status.textContent = e.message; toast(e.message); }
    }
    $("uploadBtn").disabled = false;
    $("fileInput").value = "";
    refreshDocs(toast);
    status.textContent += " · sync starts automatically within ~1 minute";
  };
  $("refreshDocsBtn").onclick = () => refreshDocs(toast);
  $("refreshJobsBtn").onclick = () => refreshJobs(toast);
  $("syncBtn").onclick = async () => {
    try { const r = await request("POST", "/kb/sync"); toast(`Sync started (${r.jobId})`); }
    catch (e) { toast(e.status === 409 ? "A sync is already running" : e.message); }
    refreshJobs(toast);
  };
}
