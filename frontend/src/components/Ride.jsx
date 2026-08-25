import { useEffect, useMemo, useRef, useState } from "react";
import { MapContainer, Marker, Popup, TileLayer, useMap } from "react-leaflet";
import L from "leaflet";
import { askAbout, reroute } from "../api.js";
import {
  bearingDeg,
  compassLabel,
  estimateRouteKm,
  formatDistance,
  formatKm,
  getBrowserLocation,
  haversine,
  interpolate,
  listenOnce,
  nodeId,
  routeLength,
  speak,
  uniqueChainIds,
} from "../geo.js";
import { nodeIcon } from "../icons.js";
import { useDebounced } from "../hooks.js";
import HereMarker from "./HereMarker.jsx";
import LocateFab from "./LocateFab.jsx";
import MapFlyTo from "./MapFlyTo.jsx";
import MapResize from "./MapResize.jsx";
import RouteLine from "./RouteLine.jsx";
import "leaflet/dist/leaflet.css";

function stopIcon(index) {
  return L.divIcon({
    className: "stop-icon",
    html: `<div class="stop-icon">${index}</div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14],
  });
}

const bikeIcon = L.divIcon({
  className: "bike-icon",
  html: `<div class="bike-icon">🚲</div>`,
  iconSize: [32, 32],
  iconAnchor: [16, 16],
});

export default function Ride({ plan, onPlanChange, onBack }) {
  const [mode, setMode] = useState("idle");
  const [position, setPosition] = useState({ lat: plan.start.lat, lng: plan.start.lng });
  const [gps, setGps] = useState(null);
  const [followGps, setFollowGps] = useState(false);
  const [locateTick, setLocateTick] = useState(0);
  const [locateBusy, setLocateBusy] = useState(false);
  const [activeId, setActiveId] = useState(plan.stops[0]?.id || null);
  const [phase, setPhase] = useState("intro");
  const [customIds, setCustomIds] = useState(() => uniqueChainIds(plan.knooppunten));
  const [rerouteBusy, setRerouteBusy] = useState(false);
  const [rerouteError, setRerouteError] = useState("");
  const [draft, setDraft] = useState(null);
  const [draftBusy, setDraftBusy] = useState(false);
  const [stepIndex, setStepIndex] = useState(0);
  const [question, setQuestion] = useState("");
  const [askBusy, setAskBusy] = useState(false);
  const [askError, setAskError] = useState("");
  const [listening, setListening] = useState(false);
  const [chats, setChats] = useState({});
  const [heading, setHeading] = useState(null);
  const [weatherOffer, setWeatherOffer] = useState(() => Boolean(plan.weather?.suggest_shorter));
  const lastGps = useRef(null);
  const spoken = useRef(new Set());
  const spokenNav = useRef(new Set());
  const spokenLocality = useRef(new Set());
  const spokenKnoop = useRef(new Set());
  const stepIndexRef = useRef(0);
  const planRef = useRef(plan);
  const watchRef = useRef(null);
  const demoRef = useRef(null);
  planRef.current = plan;

  const active = plan.stops.find((stop) => stop.id === activeId) || plan.stops[0];
  const allNodes = plan.all_knooppunten?.length ? plan.all_knooppunten : plan.knooppunten || [];
  const nodeLookup = useMemo(() => {
    const map = new Map();
    for (const node of allNodes) map.set(nodeId(node), node);
    for (const node of plan.knooppunten || []) map.set(nodeId(node), node);
    return map;
  }, [allNodes, plan.knooppunten]);
  const originalIds = useMemo(() => uniqueChainIds(plan.knooppunten).join("|"), [plan.knooppunten]);
  const dirty = customIds.join("|") !== originalIds;
  const previewKey = useDebounced(customIds.join("|"), 450);
  const selectedNodes = useMemo(
    () => customIds.map((id) => nodeLookup.get(id)).filter(Boolean),
    [customIds, nodeLookup],
  );
  const liveKm = dirty
    ? draft?.distance_km ?? estimateRouteKm(plan.start, selectedNodes, plan.mode !== "punt")
    : plan.distance_km;
  const steps = plan.steps || [];
  const currentStep = steps[stepIndex] || null;
  const stepDistance = currentStep
    ? haversine(position, { lat: currentStep.lat, lng: currentStep.lng })
    : 0;
  const chat = chats[active?.id || "live"] || [];
  const nextKnoop = useMemo(() => {
    const chain = plan.knooppunten || [];
    if (!chain.length) return null;
    let best = null;
    let bestDist = Infinity;
    for (let i = 0; i < chain.length; i += 1) {
      const node = chain[i];
      const dist = haversine(position, { lat: node.lat, lng: node.lng });
      if (dist < bestDist) {
        bestDist = dist;
        best = { node, index: i, dist };
      }
    }
    if (!best) return null;
    const upcoming = chain[Math.min(chain.length - 1, best.index + (best.dist < 80 ? 1 : 0))];
    const course =
      heading ??
      (lastGps.current
        ? bearingDeg(lastGps.current, position)
        : bearingDeg(position, { lat: upcoming.lat, lng: upcoming.lng }));
    return {
      ...upcoming,
      distance: haversine(position, { lat: upcoming.lat, lng: upcoming.lng }),
      course,
    };
  }, [heading, plan.knooppunten, position]);

  const currentLocality = useMemo(() => {
    const list = plan.localities || [];
    if (!list.length) return active?.place_name ? { name: active.place_name, fact: active.local_fact, population: active.population } : null;
    let best = list[0];
    let bestDist = Infinity;
    for (const item of list) {
      const dist = haversine(position, { lat: item.lat, lng: item.lng });
      if (dist < bestDist) {
        bestDist = dist;
        best = item;
      }
    }
    return bestDist < 2500 ? best : null;
  }, [active, plan.localities, position]);

  const guideText = useMemo(() => {
    if (phase === "intro") return plan.intro;
    if (!active) return plan.intro;
    if (phase === "approaching") return active.approaching;
    if (phase === "why") return active.why;
    return [active.arrived, active.why].filter(Boolean).join(" ");
  }, [active, phase, plan.intro]);

  useEffect(() => {
    speak(plan.intro);
  }, [plan.intro]);

  useEffect(() => {
    let cancelled = false;
    getBrowserLocation()
      .then((next) => {
        if (!cancelled) setGps(next);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setCustomIds(uniqueChainIds(plan.knooppunten));
    setStepIndex(0);
    stepIndexRef.current = 0;
    spokenNav.current = new Set();
    setPosition({ lat: plan.start.lat, lng: plan.start.lng });
    setDraft(null);
  }, [plan.knoop_chain]);

  useEffect(() => {
    if (!dirty || !previewKey) {
      setDraft(null);
      setDraftBusy(false);
      return undefined;
    }
    const picked = previewKey
      .split("|")
      .filter(Boolean)
      .map((id) => nodeLookup.get(id))
      .filter(Boolean);
    if (!picked.length) {
      setDraft(null);
      return undefined;
    }
    let cancelled = false;
    setDraftBusy(true);
    reroute({
      start_lat: plan.start.lat,
      start_lng: plan.start.lng,
      nodes: picked,
      close_loop: plan.mode !== "punt",
    })
      .then((next) => {
        if (!cancelled) setDraft(next);
      })
      .catch(() => {
        if (!cancelled) setDraft(null);
      })
      .finally(() => {
        if (!cancelled) setDraftBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [dirty, nodeLookup, plan.mode, plan.start.lat, plan.start.lng, previewKey]);

  useEffect(() => {
    return () => {
      stopTracking();
      window.speechSynthesis?.cancel();
    };
  }, []);

  function stopTracking() {
    if (watchRef.current != null) {
      navigator.geolocation.clearWatch(watchRef.current);
      watchRef.current = null;
    }
    if (demoRef.current) {
      cancelAnimationFrame(demoRef.current);
      demoRef.current = null;
    }
    setMode("idle");
    setFollowGps(false);
  }

  function onMove(here, fromGps = false) {
    if (fromGps && lastGps.current) {
      const moved = haversine(lastGps.current, here);
      if (moved > 8) setHeading(bearingDeg(lastGps.current, here));
    }
    if (fromGps) lastGps.current = here;
    setPosition(here);
    if (fromGps) {
      setGps((current) => ({ ...here, accuracy: current?.accuracy || 25 }));
    }
    maybeAnnounce(here);
    advanceNav(here);
    announceGeoContext(here);
  }

  async function showMyLocation() {
    setLocateBusy(true);
    try {
      const next = await getBrowserLocation();
      setGps(next);
      setFollowGps(true);
      setLocateTick((tick) => tick + 1);
      setRerouteError("");
    } catch (err) {
      setRerouteError(err.message);
    } finally {
      setLocateBusy(false);
    }
  }

  async function startLive() {
    stopTracking();
    setMode("live");
    setPhase("intro");
    setFollowGps(true);
    try {
      await navigator.wakeLock?.request("screen");
    } catch {
      // optional
    }
    if (!navigator.geolocation) {
      setMode("idle");
      setRerouteError("GPS is niet beschikbaar in deze browser.");
      return;
    }
    try {
      const first = await getBrowserLocation();
      onMove(first, true);
    } catch (err) {
      setRerouteError(err.message);
    }
    watchRef.current = navigator.geolocation.watchPosition(
      (pos) => onMove({ lat: pos.coords.latitude, lng: pos.coords.longitude }, true),
      () => setRerouteError("GPS-toegang geweigerd of tijdelijk niet beschikbaar."),
      { enableHighAccuracy: true, maximumAge: 1000, timeout: 12000 },
    );
  }

  function startDemo() {
    stopTracking();
    setMode("demo");
    const total = routeLength(plan.geometry);
    const started = performance.now();
    const duration = Math.min(90000, Math.max(25000, total / 2));

    const tick = (now) => {
      const t = Math.min(1, (now - started) / duration);
      const along = interpolate(plan.geometry, total * t);
      if (along) onMove(along);
      if (t < 1) demoRef.current = requestAnimationFrame(tick);
      else setMode("idle");
    };
    demoRef.current = requestAnimationFrame(tick);
  }

  function maybeAnnounce(here) {
    const stops = planRef.current.stops || [];
    let closest = null;
    let best = Infinity;
    for (const stop of stops) {
      const distance = haversine(here, { lat: stop.lat, lng: stop.lng });
      if (distance < best) {
        best = distance;
        closest = stop;
      }
    }
    if (!closest) return;
    setActiveId(closest.id);
    if (best < 280 && !spoken.current.has(`${closest.id}-arrived`)) {
      spoken.current.add(`${closest.id}-arrived`);
      setPhase("arrived");
      speak(`${closest.arrived} ${closest.why || ""}`.trim());
    } else if (best < 700 && !spoken.current.has(`${closest.id}-near`)) {
      spoken.current.add(`${closest.id}-near`);
      setPhase("approaching");
      speak(closest.approaching);
    }
  }

  function advanceNav(here) {
    const navSteps = planRef.current.steps || [];
    if (!navSteps.length) return;
    let index = stepIndexRef.current;
    let nearest = index;
    let nearestDist = Infinity;
    for (let i = Math.max(0, index - 1); i < navSteps.length; i += 1) {
      const dist = haversine(here, { lat: navSteps[i].lat, lng: navSteps[i].lng });
      if (dist < nearestDist) {
        nearestDist = dist;
        nearest = i;
      }
    }
    const currentDist = haversine(here, { lat: navSteps[index].lat, lng: navSteps[index].lng });
    if (nearest > index && nearestDist + 40 < currentDist) {
      index = nearest;
    }
    const hereDist = haversine(here, { lat: navSteps[index].lat, lng: navSteps[index].lng });
    const step = navSteps[index];
    if (hereDist < 90 && !spokenNav.current.has(index) && step.type !== "depart") {
      spokenNav.current.add(index);
      speak(step.instruction);
    }
    if (hereDist < 28 && index < navSteps.length - 1) {
      index += 1;
    }
    if (index !== stepIndexRef.current) {
      stepIndexRef.current = index;
      setStepIndex(index);
    }
  }

  function toggleNode(node) {
    const id = nodeId(node);
    setCustomIds((current) => {
      if (current.includes(id)) return current.filter((item) => item !== id);
      return [...current, id];
    });
    setRerouteError("");
  }

  async function applyCustomRoute() {
    const selected = customIds.map((id) => nodeLookup.get(id)).filter(Boolean);
    if (!selected.length) {
      setRerouteError("Selecteer minstens één knooppunt.");
      return;
    }
    setRerouteBusy(true);
    setRerouteError("");
    try {
      const next = await reroute({
        start_lat: plan.start.lat,
        start_lng: plan.start.lng,
        nodes: selected,
        close_loop: plan.mode !== "punt",
      });
      const selectedIds = new Set(customIds);
      onPlanChange({
        ...plan,
        ...next,
        all_knooppunten: allNodes.map((node) => ({
          ...node,
          on_route: selectedIds.has(nodeId(node)),
        })),
        route_reason: "Eigen knooppuntenroute",
      });
    } catch (err) {
      setRerouteError(err.message);
    } finally {
      setRerouteBusy(false);
    }
  }

  function announceGeoContext(here) {
    const chain = planRef.current.knooppunten || [];
    for (const node of chain) {
      const dist = haversine(here, { lat: node.lat, lng: node.lng });
      const key = `knoop-${nodeId(node)}`;
      if (dist < 120 && !spokenKnoop.current.has(key)) {
        spokenKnoop.current.add(key);
        speak(`Knooppunt ${node.number} komt eraan.`);
        break;
      }
    }
    for (const place of planRef.current.localities || []) {
      const dist = haversine(here, { lat: place.lat, lng: place.lng });
      const key = `place-${place.name}`;
      if (dist < 700 && !spokenLocality.current.has(key)) {
        spokenLocality.current.add(key);
        const pop = place.population ? `, ongeveer ${place.population} inwoners` : "";
        const fact = place.fact ? ` ${place.fact}` : "";
        speak(`Je komt in ${place.name}${pop}.${fact}`);
        break;
      }
    }
  }

  async function applyAdaptation(reason) {
    const selected = (plan.knooppunten || []).filter((node, index, arr) => {
      if (!node) return false;
      if (index > 0 && nodeId(node) === nodeId(arr[0]) && index === arr.length - 1) return false;
      return true;
    });
    if (!selected.length) {
      setRerouteError("Geen knooppunten om aan te passen.");
      return;
    }
    setRerouteBusy(true);
    setRerouteError("");
    try {
      const next = await reroute({
        start_lat: position.lat,
        start_lng: position.lng,
        nodes: selected,
        close_loop: plan.mode !== "punt",
        reason,
        target_km: Math.max(8, (plan.distance_km || 20) * 0.6),
        interests: plan.interests || [],
      });
      onPlanChange({
        ...plan,
        ...next,
        all_knooppunten: allNodes.map((node) => ({
          ...node,
          on_route: next.knooppunten.some((item) => nodeId(item) === nodeId(node)),
        })),
        route_reason: next.reason || plan.route_reason,
        weather: next.weather || plan.weather,
      });
      setWeatherOffer(false);
      setCustomIds(uniqueChainIds(next.knooppunten));
      speak(next.reason || "Ik heb een aangepaste knooppuntenroute klaargezet.");
    } catch (err) {
      setRerouteError(err.message);
    } finally {
      setRerouteBusy(false);
    }
  }

  async function submitQuestion(event) {
    event.preventDefault();
    if (plan.interaction === "passief") return;
    if (question.trim().length < 2) return;
    setAskBusy(true);
    setAskError("");
    try {
      const data = await askAbout({
        question: question.trim(),
        name: active?.name || "",
        kind: active?.kind || "",
        summary: active?.summary || "",
        arrived: active?.arrived || "",
        explanation_level: plan.explanation_level || "normaal",
        lat: position.lat,
        lng: position.lng,
        heading,
        place_name: currentLocality?.name || active?.place_name || "",
        interests: plan.interests || [],
      });
      const key = active?.id || "live";
      setChats((current) => ({
        ...current,
        [key]: [...(current[key] || []), { q: question.trim(), a: data.answer }],
      }));
      setQuestion("");
      speak(data.answer);
    } catch (err) {
      setAskError(err.message);
    } finally {
      setAskBusy(false);
    }
  }

  async function askByVoice() {
    if (plan.interaction === "passief") return;
    setAskError("");
    setListening(true);
    try {
      const text = await listenOnce("nl-BE");
      setQuestion(text);
      setListening(false);
      setAskBusy(true);
      const data = await askAbout({
        question: text,
        name: active?.name || "",
        kind: active?.kind || "",
        summary: active?.summary || "",
        arrived: active?.arrived || "",
        explanation_level: plan.explanation_level || "normaal",
        lat: position.lat,
        lng: position.lng,
        heading,
        place_name: currentLocality?.name || active?.place_name || "",
        interests: plan.interests || [],
      });
      const key = active?.id || "live";
      setChats((current) => ({
        ...current,
        [key]: [...(current[key] || []), { q: text, a: data.answer }],
      }));
      setQuestion("");
      speak(data.answer);
    } catch (err) {
      setAskError(err.message);
    } finally {
      setListening(false);
      setAskBusy(false);
    }
  }

  function nodeVariant(node) {
    const id = nodeId(node);
    if (customIds.includes(id)) return node.on_route && !dirty ? "route" : "picked";
    if (node.on_route) return "route";
    return "idle";
  }

  return (
    <div className="ride">
      <aside className="sidebar">
        <div>
          <div className="eyebrow">Je tocht</div>
          <h2 className="brand" style={{ fontSize: "1.35rem", marginTop: 6 }}>
            {plan.title}
          </h2>
        </div>
        {plan.ai_used && (
          <div className="guide-pill">
            Persoonlijke gids · {plan.explanation_level || "normaal"}
            {plan.interaction === "passief" ? " · luisteren" : " · live vragen"}
          </div>
        )}
        {plan.weather?.summary && (
          <div className={`weather-pill ${plan.weather.suggest_shorter ? "warn" : ""}`}>
            {plan.weather.summary}
            {plan.weather.alert ? ` · ${plan.weather.alert}` : ""}
          </div>
        )}
        {weatherOffer && (
          <div className="editor">
            <strong>Weer-aanpassing</strong>
            <p className="sources" style={{ margin: "6px 0 8px" }}>
              {plan.weather?.alert || "Het weer maakt een kortere knooppuntenroute verstandiger."}
            </p>
            <div className="actions">
              <button type="button" onClick={() => applyAdaptation("regen")} disabled={rerouteBusy}>
                Kortere route
              </button>
              <button type="button" className="ghost" onClick={() => applyAdaptation("veer")} disabled={rerouteBusy}>
                Vermijd veer
              </button>
              <button type="button" className="ghost" onClick={() => setWeatherOffer(false)}>
                Behouden
              </button>
            </div>
          </div>
        )}
        {nextKnoop && (
          <div className="context-card">
            <div className="kicker">Volgend knooppunt</div>
            <strong>
              {nextKnoop.number} · {formatDistance(nextKnoop.distance)} · {compassLabel(nextKnoop.course)}
            </strong>
            {currentLocality && (
              <p>
                Je bent in {currentLocality.name}
                {currentLocality.population ? ` (${currentLocality.population} inwoners)` : ""}.
                {currentLocality.fact ? ` ${currentLocality.fact}` : ""}
              </p>
            )}
          </div>
        )}
        {plan.route_reason && <p className="sources">{plan.route_reason}</p>}
        {plan.knoop_chain && (
          <>
            <p className="sources" style={{ margin: "8px 0 6px" }}>
              Te volgen knooppunten ({plan.knooppunten.length})
            </p>
            <ol className="picked-list route-knoop-list">
              {plan.knooppunten.map((node, index) => (
                <li key={`${nodeId(node)}-${index}`}>
                  <span className="num">{index + 1}</span>
                  <span>
                    <strong>Knooppunt {node.number}</strong>
                  </span>
                </li>
              ))}
            </ol>
          </>
        )}
        <div className="stats">
          <div className="stat">
            <span>{dirty ? "Voorlopige afstand" : "Afstand"}</span>
            <b>{typeof liveKm === "number" ? formatKm(liveKm) : `${plan.distance_km} km`}</b>
          </div>
          <div className="stat">
            <span>Rijtijd</span>
            <b>{dirty ? (draft?.duration_min ? `${draft.duration_min} min` : "…") : `${plan.duration_min} min`}</b>
          </div>
          <div className="stat">
            <span>Knooppunten</span>
            <b>{customIds.length}</b>
          </div>
        </div>
        <div className="actions">
          <button type="button" onClick={startLive}>
            Start route
          </button>
          <button type="button" className="ghost" onClick={showMyLocation}>
            Mijn locatie
          </button>
          <button type="button" onClick={startDemo}>
            Simuleer rit
          </button>
          {mode !== "idle" && (
            <button className="ghost" type="button" onClick={stopTracking}>
              Stop
            </button>
          )}
          <button className="ghost" type="button" onClick={onBack}>
            Nieuwe route
          </button>
        </div>
        <p className="sources">
          {mode === "live"
            ? "GPS-navigatie aan: afslagen en uitleg worden voorgelezen."
            : mode === "demo"
              ? "Demo: de fiets rijdt de route af met navigatie en uitleg."
              : "Start route voor GPS-begeleiding. De blauwe punt is jouw locatie."}
        </p>
        <div className="editor">
          <strong>Eigen route</strong>
          <p className="sources" style={{ margin: "6px 0 8px" }}>
            Grijze nummers liggen in de buurt. Klik om ze toe te voegen of te schrappen. De kilometers
            worden meteen bijgewerkt.
          </p>
          {customIds.length ? (
            <ol className="picked-list">
              {customIds.map((id, index) => {
                const node = nodeLookup.get(id);
                if (!node) return null;
                return (
                  <li key={id}>
                    <span className="num">{index + 1}</span>
                    <span>
                      <strong>Knooppunt {node.number}</strong>
                    </span>
                    <button type="button" className="ghost-mini" onClick={() => toggleNode(node)}>
                      ×
                    </button>
                  </li>
                );
              })}
            </ol>
          ) : (
            <p className="sources" style={{ margin: 0 }}>
              Nog geen knooppunten gekozen.
            </p>
          )}
          <div className="chain">
            {customIds.map((id, index) => {
              const node = nodeLookup.get(id);
              if (!node) return null;
              return (
                <span key={id} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  {index > 0 && <span className="node-arrow">→</span>}
                  <button
                    type="button"
                    className={`node ${node.on_route ? "" : "picked"}`}
                    onClick={() => toggleNode(node)}
                    title="Verwijder uit je route"
                  >
                    {node.number}
                  </button>
                </span>
              );
            })}
          </div>
          {draftBusy && dirty && <p className="sources" style={{ margin: 0 }}>Fietsroute wordt herberekend…</p>}
          {dirty && (
            <button className="submit" type="button" onClick={applyCustomRoute} disabled={rerouteBusy}>
              {rerouteBusy ? "Route wordt herberekend..." : "Neem deze knooppunten over"}
            </button>
          )}
        </div>
        {rerouteError && <div className="error">{rerouteError}</div>}
        <div className="stop-list">
          {plan.stops.map((stop, index) => (
            <button
              key={stop.id}
              type="button"
              className={`stop ${stop.id === activeId ? "active" : ""}`}
              onClick={() => {
                setActiveId(stop.id);
                setPhase("arrived");
                speak(`${stop.arrived} ${stop.why || ""}`.trim());
              }}
            >
              <span className="num">{index + 1}</span>
              <span>
                <strong>{stop.name}</strong>
                <br />
                <small>
                  {stop.kind} · {stop.source}
                </small>
              </span>
            </button>
          ))}
        </div>
        {plan.interaction !== "passief" && (
          <form className="ask" onSubmit={submitQuestion}>
            <strong>Live vraag aan de gids</strong>
            {chat.map((item, index) => (
              <div key={`${item.q}-${index}`} className="qa">
                <p>
                  <b>Jij:</b> {item.q}
                </p>
                <p>{item.a}</p>
              </div>
            ))}
            <textarea
              rows="2"
              placeholder='Bijvoorbeeld: "Wat is dat gebouw aan mijn rechterkant?"'
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
            />
            {askError && <div className="error">{askError}</div>}
            <div className="actions">
              <button className="submit" type="submit" disabled={askBusy || question.trim().length < 2}>
                {askBusy ? "De gids denkt na..." : "Stel vraag"}
              </button>
              <button type="button" className="ghost" onClick={askByVoice} disabled={askBusy || listening}>
                {listening ? "Luisteren..." : "Spraak"}
              </button>
            </div>
          </form>
        )}
        <p className="sources">Bronnen: {plan.sources.join(" · ")}</p>
      </aside>

      <section className="map-wrap">
        <MapContainer center={[plan.start.lat, plan.start.lng]} zoom={13} scrollWheelZoom>
          <TileLayer
            attribution="&copy; OpenStreetMap &copy; CyclOSM"
            url="https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png"
          />
          <MapResize />
          <RouteLine positions={plan.geometry} opacity={dirty ? 0.45 : 1} />
          {dirty && draft?.geometry?.length > 1 && (
            <RouteLine positions={draft.geometry} color="#4f8f43" dashed />
          )}
          {allNodes.map((node) => {
            const variant = nodeVariant(node);
            return (
              <Marker
                key={nodeId(node)}
                position={[node.lat, node.lng]}
                icon={nodeIcon(node.number, variant)}
                zIndexOffset={variant === "idle" ? 800 : 1200}
                eventHandlers={{
                  click: (event) => {
                    if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
                    toggleNode(node);
                  },
                }}
              >
                <Popup>
                  <strong>Knooppunt {node.number}</strong>
                  <p>{customIds.includes(nodeId(node)) ? "Op je route. Klik om te schrappen." : "Klik om langs hier te fietsen."}</p>
                </Popup>
              </Marker>
            );
          })}
          {plan.stops.map((stop, index) => (
            <Marker key={stop.id} position={[stop.lat, stop.lng]} icon={stopIcon(index + 1)}>
              <Popup>
                <strong>{stop.name}</strong>
                <p>{stop.arrived}</p>
                {stop.wikipedia_url && (
                  <a href={stop.wikipedia_url} target="_blank" rel="noreferrer">
                    Wikipedia
                  </a>
                )}
              </Popup>
            </Marker>
          ))}
          {mode === "demo" && <Marker position={[position.lat, position.lng]} icon={bikeIcon} />}
          <HereMarker position={gps} accuracy={gps?.accuracy} />
          <MapFlyTo position={gps} trigger={locateTick} zoom={16} />
          <FitRoute geometry={plan.geometry} nodes={allNodes} />
          <Follow position={mode === "demo" ? position : gps || position} enabled={mode === "demo" || followGps} />
        </MapContainer>
        <div className="map-overlay">
          <LocateFab onClick={showMyLocation} disabled={locateBusy} busy={locateBusy} />
        </div>
        {currentStep && mode !== "idle" && (
          <div className="nav-banner">
            <div className="kicker">
              {nextKnoop
                ? `Knooppunt ${nextKnoop.number} · ${formatDistance(nextKnoop.distance)} · ${compassLabel(nextKnoop.course)}`
                : `Volgende manoeuvre · ${formatDistance(stepDistance)}`}
            </div>
            <p>{currentStep.instruction}</p>
            {currentLocality && <small>{currentLocality.name}{currentLocality.population ? ` · ${currentLocality.population} inwoners` : ""}</small>}
          </div>
        )}
        {weatherOffer && mode !== "idle" && (
          <div className="weather-banner">
            <div className="kicker">Weer</div>
            <p>{plan.weather?.alert || "Kortere route aangeraden."}</p>
            <button type="button" onClick={() => applyAdaptation("regen")} disabled={rerouteBusy}>
              Pas route aan
            </button>
          </div>
        )}
        <div className="guide">
          <div className="kicker">
            {phase === "intro"
              ? "Je gids"
              : active
                ? `${active.name}${active.place_name ? ` · ${active.place_name}` : ""} · ${active.kind}`
                : "Je gids"}
          </div>
          <p>{guideText}</p>
        </div>
      </section>
    </div>
  );
}

function FitRoute({ geometry, nodes }) {
  const map = useMap();
  useEffect(() => {
    const points = [...(geometry || [])];
    for (const node of nodes || []) points.push([node.lat, node.lng]);
    if (!points.length) return;
    map.fitBounds(points, { padding: [36, 36] });
  }, [geometry, nodes, map]);
  return null;
}

function Follow({ position, enabled }) {
  const map = useMap();
  useEffect(() => {
    if (enabled) map.panTo([position.lat, position.lng]);
  }, [enabled, map, position]);
  return null;
}
