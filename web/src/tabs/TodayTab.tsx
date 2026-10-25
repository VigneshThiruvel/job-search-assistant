import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Card } from "../api/client";
import { useAction } from "./useRefresh";

const ORDER = { apply: 0, maybe: 1, skip: 2 } as const;

export default function TodayTab() {
  const queued = useQuery({ queryKey: ["applications", "queued"], queryFn: () => api.applications("queued") });
  if (queued.isPending) return <p>Loading…</p>;
  if (queued.isError) return <p className="error">{queued.error.message}</p>;

  const sorted = [...queued.data].sort((a, b) => ORDER[a.recommendation] - ORDER[b.recommendation]);
  const lists = [
    { title: "LinkedIn / XING", hint: "where I apply", cards: sorted.filter((c) => c.channel === "alerts") },
    { title: "Other sites", hint: "strong matches only", cards: sorted.filter((c) => c.channel === "other") },
  ];
  return (
    <div className="columns">
      {lists.map((l) => (
        <section key={l.title}>
          <h2>
            {l.title} <small>{l.hint} · {l.cards.length}</small>
          </h2>
          {l.cards.length === 0 && <p className="muted">Nothing to review.</p>}
          {l.cards.map((c) => (
            <JobCard key={c.job_id} card={c} />
          ))}
        </section>
      ))}
    </div>
  );
}

function JobCard({ card }: { card: Card }) {
  const [note, setNote] = useState("");
  const [letter, setLetter] = useState(false);
  const select = useAction(api.select);
  const skip = useAction(api.skip);
  const error = select.error ?? skip.error;

  return (
    <article className="card">
      <div className="card-head">
        <span className={`rec rec-${card.recommendation}`}>{card.recommendation}</span>
        <a href={card.apply_url} target="_blank" rel="noreferrer">
          <strong>{card.title}</strong>
        </a>
      </div>
      <div className="muted">
        {card.company} · {card.location ?? "?"} · #{card.job_id}
      </div>
      <p>{card.fit_summary}</p>
      <div className="actions">
        <input placeholder="Note (what to stress / why skip)" value={note} onChange={(e) => setNote(e.target.value)} />
        <label>
          <input type="checkbox" checked={letter} onChange={(e) => setLetter(e.target.checked)} /> letter
        </label>
        <button onClick={() => select.mutate([card.job_id, letter, note || null])} disabled={select.isPending}>
          Select
        </button>
        <button className="secondary" onClick={() => skip.mutate([card.job_id, note || null])} disabled={skip.isPending}>
          Skip
        </button>
      </div>
      {error && <p className="error">{error.message}</p>}
    </article>
  );
}
