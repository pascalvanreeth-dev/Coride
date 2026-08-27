import { useMemo, useState } from "react";
import {
  AGE_BANDS,
  BIKES,
  COMMENTARY,
  FITNESS,
  HORECA,
  INTERACTION,
  THEMES,
  defaultProfile,
} from "../profile.js";

const STEPS = [
  { id: "basis", title: "Wie fietst er mee?", lede: "Zo past tempo, afstand en toon van de gids bij jou." },
  { id: "themas", title: "Waar wil je langs?", lede: "Kies alles wat je onderweg wilt horen of zien." },
  { id: "horeca", title: "Horeca onderweg?", lede: "Optioneel. We zoeken stops die bij je beleving passen." },
  { id: "gids", title: "Hoe mag de gids klinken?", lede: "Kies hoeveel verhaal je hoort, en of je mag bijvragen." },
];

export default function Onboarding({ initial, onComplete }) {
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState(() => ({ ...defaultProfile(), ...initial, completed: false }));

  const current = STEPS[step];
  const canNext = useMemo(() => {
    if (step === 0) return Boolean(draft.ageBand && draft.fitness && draft.bike);
    if (step === 1) return (draft.interests || []).length > 0;
    return true;
  }, [draft, step]);

  function patch(partial) {
    setDraft((currentDraft) => ({ ...currentDraft, ...partial }));
  }

  function toggleList(key, id, exclusiveNone = false) {
    setDraft((currentDraft) => {
      const list = currentDraft[key] || [];
      if (exclusiveNone && id === "geen") return { ...currentDraft, [key]: [] };
      const next = list.includes(id) ? list.filter((item) => item !== id) : [...list, id];
      return { ...currentDraft, [key]: next };
    });
  }

  function finish() {
    onComplete({ ...draft, completed: true });
  }

  return (
    <div className="onboard">
      <div className="onboard-card">
        <div className="onboard-scroll">
          <div className="eyebrow">Welkom bij Veloverhaal · stap {step + 1} van {STEPS.length}</div>
          <div className="step-dots" aria-hidden="true">
            {STEPS.map((item, index) => (
              <span key={item.id} className={index <= step ? "on" : ""} />
            ))}
          </div>
          <div>
            <h1 className="brand" style={{ fontSize: "1.7rem" }}>
              {current.title}
            </h1>
            <p className="lede">{current.lede}</p>
          </div>

          {step === 0 && (
          <>
            <Section title="Leeftijd">
              <div className="row">
                {AGE_BANDS.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`interest ${draft.ageBand === item.id ? "on" : ""}`}
                    onClick={() => patch({ ageBand: item.id })}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </Section>
            <Section title="Fysieke paraatheid">
              <div className="choice">
                {FITNESS.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`choice-card ${draft.fitness === item.id ? "on" : ""}`}
                    onClick={() => patch({ fitness: item.id })}
                  >
                    <strong>{item.label}</strong>
                    <span>{item.hint}</span>
                  </button>
                ))}
              </div>
            </Section>
            <Section title="Type fiets">
              <div className="onboard-grid">
                {BIKES.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`choice-card ${draft.bike === item.id ? "on" : ""}`}
                    onClick={() => patch({ bike: item.id })}
                  >
                    <strong>{item.label}</strong>
                    <span>{item.hint}</span>
                  </button>
                ))}
              </div>
            </Section>
          </>
        )}

        {step === 1 && (
          <div className="onboard-grid">
            {THEMES.map((item) => (
              <button
                key={item.id}
                type="button"
                className={`choice-card ${draft.interests.includes(item.id) ? "on" : ""}`}
                onClick={() => toggleList("interests", item.id)}
              >
                <strong>{item.label}</strong>
                <span>{item.hint}</span>
              </button>
            ))}
          </div>
        )}

        {step === 2 && (
          <>
            <button
              type="button"
              className={`choice-card ${draft.horeca.length === 0 ? "on" : ""}`}
              onClick={() => patch({ horeca: [] })}
            >
              <strong>Geen horeca nodig</strong>
              <span>Gewoon fietsen. Eventueel later nog een stop kiezen.</span>
            </button>
            <div className="onboard-grid">
              {HORECA.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={`choice-card ${draft.horeca.includes(item.id) ? "on" : ""}`}
                  onClick={() => toggleList("horeca", item.id)}
                >
                  <strong>{item.label}</strong>
                  <span>{item.hint}</span>
                </button>
              ))}
            </div>
          </>
        )}

        {step === 3 && (
          <>
            <Section title="Uitgebreidheid van commentaar">
              <div className="choice">
                {COMMENTARY.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`choice-card ${draft.commentary === item.id ? "on" : ""}`}
                    onClick={() => patch({ commentary: item.id })}
                  >
                    <strong>{item.label}</strong>
                    <span>{item.hint}</span>
                  </button>
                ))}
              </div>
            </Section>
            <Section title="Interactie met de gids">
              <div className="choice">
                {INTERACTION.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`choice-card ${draft.interaction === item.id ? "on" : ""}`}
                    onClick={() => patch({ interaction: item.id })}
                  >
                    <strong>{item.label}</strong>
                    <span>{item.hint}</span>
                  </button>
                ))}
              </div>
            </Section>
          </>
        )}

          {!canNext && <div className="error">Kies minstens één optie om verder te gaan.</div>}
        </div>

        <div className="onboard-nav">
          {step > 0 ? (
            <button type="button" className="ghost-link" onClick={() => setStep((n) => n - 1)}>
              Terug
            </button>
          ) : (
            <span />
          )}
          {step < STEPS.length - 1 ? (
            <button type="button" className="submit onboard-next" disabled={!canNext} onClick={() => setStep((n) => n + 1)}>
              Volgende
            </button>
          ) : (
            <button type="button" className="submit onboard-next" onClick={finish}>
              Profiel opslaan
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }) {
  return (
    <div className="onboard-section">
      <strong>{title}</strong>
      {children}
    </div>
  );
}
