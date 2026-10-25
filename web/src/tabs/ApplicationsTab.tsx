import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type Card, type Detail, type Status } from "../api/client";
import { useAction } from "./useRefresh";

const GROUPS: { status: Status; title: string }[] = [
  { status: "selected", title: "Drafting (run prepare_applications)" },
  { status: "ready", title: "Ready to send" },
  { status: "applied", title: "Applied" },
  { status: "skipped", title: "Skipped" },
];

export default function ApplicationsTab() {
  const all = useQuery({ queryKey: ["applications", "all"], queryFn: () => api.applications() });
  const [open, setOpen] = useState<number | null>(null);
  if (all.isPending) return <p>Loading…</p>;
  if (all.isError) return <p className="error">{all.error.message}</p>;

  return (
    <div className="split">
      <aside>
        {GROUPS.map((g) => {
          const cards = all.data.filter((c) => c.status === g.status);
          return (
            <section key={g.status}>
              <h3>
                {g.title} <small>{cards.length}</small>
              </h3>
              {cards.map((c) => (
                <button
                  key={c.job_id}
                  className={`row ${open === c.job_id ? "active" : ""}`}
                  onClick={() => setOpen(c.job_id)}
                >
                  <span>{c.title}</span>
                  <small className="muted">
                    {c.company} {c.channel === "alerts" ? "· LinkedIn/XING" : ""}
                  </small>
                </button>
              ))}
            </section>
          );
        })}
      </aside>
      <section className="detail">{open ? <DetailView jobId={open} /> : <p className="muted">Pick a job.</p>}</section>
    </div>
  );
}

function DetailView({ jobId }: { jobId: number }) {
  const detail = useQuery({ queryKey: ["application", jobId], queryFn: () => api.application(jobId) });
  if (detail.isPending) return <p>Loading…</p>;
  if (detail.isError) return <p className="error">{detail.error.message}</p>;
  const d = detail.data;
  return (
    <>
      <h2>
        <a href={d.card.apply_url} target="_blank" rel="noreferrer">
          {d.card.title}
        </a>{" "}
        <small>
          {d.card.company} · {d.card.status}
        </small>
      </h2>
      <Actions card={d.card} />
      <p>{d.card.fit_summary}</p>
      {d.card.note && <p className="note">My note: {d.card.note}</p>}
      {d.resume ? <ResumeView resume={d.resume} jobId={jobId} /> : <p className="muted">No tailored résumé yet.</p>}
      {d.letter && <LetterView detail={d} />}
      <details>
        <summary>Posting</summary>
        <pre className="posting">{d.description || "(no description)"}</pre>
      </details>
    </>
  );
}

function Actions({ card }: { card: Card }) {
  const [note, setNote] = useState("");
  const select = useAction(api.select);
  const letter = useAction(api.letter);
  const applied = useAction(api.applied);
  const skip = useAction(api.skip);
  const closed = card.status === "applied";
  const error = select.error ?? letter.error ?? applied.error ?? skip.error;
  const n = note || null;
  return (
    <div className="actions">
      <input placeholder="Note" value={note} onChange={(e) => setNote(e.target.value)} disabled={closed} />
      <button onClick={() => applied.mutate([card.job_id, n])} disabled={closed}>
        I applied
      </button>
      <button className="secondary" onClick={() => select.mutate([card.job_id, false, n])} disabled={closed}>
        Redraft résumé
      </button>
      <button className="secondary" onClick={() => letter.mutate([card.job_id, n])} disabled={closed}>
        {card.has_letter ? "Redraft letter" : "Request letter"}
      </button>
      <button className="secondary" onClick={() => skip.mutate([card.job_id, n])} disabled={closed || card.status === "skipped"}>
        Skip
      </button>
      {error && <p className="error">{error.message}</p>}
    </div>
  );
}

function ResumeView({ resume, jobId }: { resume: NonNullable<Detail["resume"]>; jobId: number }) {
  return (
    <section>
      <h3>
        Tailored résumé{" "}
        <a href={api.resumePdfUrl(jobId)} target="_blank" rel="noreferrer">
          PDF
        </a>
      </h3>
      <div className="compare">
        <div>
          <h4>Summary before</h4>
          <p>{resume.summary_before}</p>
        </div>
        <div>
          <h4>Summary after</h4>
          <p>{resume.summary_after}</p>
        </div>
      </div>
      {resume.entries.map((e) => (
        <div key={e.id}>
          <h4>{e.heading}</h4>
          <ul>
            {e.bullets.map((b, i) => (
              <li key={i}>
                {b.text}
                {b.sources.map((s) =>
                  s.text === b.text ? null : (
                    <div key={s.id} className="was">
                      was {s.id}: {s.text}
                    </div>
                  ),
                )}
              </li>
            ))}
          </ul>
        </div>
      ))}
      {resume.skills && (
        <>
          <h4>Skills (new order)</h4>
          <ul>
            {resume.skills.map((s) => (
              <li key={s.label}>
                <strong>{s.label}:</strong> {s.items.join(", ")}
              </li>
            ))}
          </ul>
        </>
      )}
      {resume.dropped.length > 0 && (
        <>
          <h4>Left out</h4>
          <ul className="muted">
            {resume.dropped.map((x) => (
              <li key={x}>{x}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function LetterView({ detail }: { detail: Detail }) {
  return (
    <section>
      <h3>Cover letter</h3>
      <pre className="letter">{detail.letter}</pre>
      <h4>Claims</h4>
      <ul>
        {detail.claims.map((c, i) => (
          <li key={i}>
            {c.claim}
            <div className="was">source: {c.evidence}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}
