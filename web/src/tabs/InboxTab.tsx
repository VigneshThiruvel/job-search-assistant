import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type InboxItem } from "../api/client";
import { useAction } from "./useRefresh";

export default function InboxTab() {
  const items = useQuery({ queryKey: ["inbox"], queryFn: api.inbox });
  if (items.isPending) return <p>Loading…</p>;
  if (items.isError) return <p className="error">{items.error.message}</p>;
  return (
    <section>
      <h2>
        Paste descriptions <small>LinkedIn/XING are never scraped: open the link, copy, paste here</small>
      </h2>
      {items.data.length === 0 && <p className="muted">Nothing to paste. New files appear after <code>daily</code>.</p>}
      {items.data.map((item) => (
        <PasteForm key={item.job_id} item={item} />
      ))}
    </section>
  );
}

function PasteForm({ item }: { item: InboxItem }) {
  const [description, setDescription] = useState("");
  const [notes, setNotes] = useState("");
  const [expired, setExpired] = useState(false);
  const save = useAction(api.paste);

  return (
    <article className="card">
      <div className="card-head">
        <span className="rec">{item.source}</span>
        <a href={item.url} target="_blank" rel="noreferrer">
          <strong>{item.title}</strong>
        </a>
      </div>
      <div className="muted">
        {item.company} · {item.location ?? "?"} · #{item.job_id} · email {item.posted_at?.slice(0, 10)}
      </div>
      <textarea
        rows={6}
        placeholder="Paste the full job description"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      <div className="actions">
        <input placeholder="Notes (shown to the agent)" value={notes} onChange={(e) => setNotes(e.target.value)} />
        <label>
          <input type="checkbox" checked={expired} onChange={(e) => setExpired(e.target.checked)} /> expired
        </label>
        <button
          onClick={() => save.mutate([item.job_id, { description, notes: notes || null, expired }])}
          disabled={save.isPending || (!expired && description.trim().length === 0)}
        >
          Save
        </button>
      </div>
      {save.error && <p className="error">{save.error.message}</p>}
    </article>
  );
}
