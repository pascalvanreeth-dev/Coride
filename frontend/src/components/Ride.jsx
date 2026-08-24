import { useEffect, useMemo, useRef, useState } from "react";
import { MapContainer, Marker, Polyline, Popup, TileLayer, useMap } from "react-leaflet";
import L from "leaflet";
import { askAbout, reroute } from "../api.js";
import { formatDistance, getBrowserLocation, haversine, interpolate, nodeId, routeLength, speak, uniqueChainIds } from "../geo.js";
import HereMarker from "./HereMarker.jsx";
import "leaflet/dist/leaflet.css";

function stopIcon(index) {
  return L.divIcon({
    className: "stop-icon",
    html: `<div class="stop-icon">${index}</div>`,
    iconSize: [28, 28],
    iconAnchor: [14, 14],
  });
}

function nodeIcon(number, variant) {
  const size = variant === "idle" ? 24 : 34;
  return L.divIcon({
    className: `node-icon ${variant}`,
    html: `<div class="node-icon ${variant}">${number}</div>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
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
  const [activeId, setActiveId] = useState(plan.stops[0]?.id || null);
  const [phase, setPhase] = useState("intro");
  const [customIds, setCustomIds] = useState(() => uniqueChainIds(plan.knooppunten));
  const [rerouteBusy, setRerouteBusy] = useState(false);
  const [rerouteError, setRerouteError] = useState("");
  const [stepIndex, setStepIndex] = useState(0);
  const [question, setQuestion] = useState("");
  const [askBusy, setAskBusy] = useState(false);
  const [askError, setAskError] = useState("");
  const [chats, setChats] = useState({});
  const spoken = useRef(new Set());
  const spokenNav = useRef(new Set());
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
  const steps = plan.steps || [];
  const currentStep = steps[stepIndex] || null;
  const stepDistance = currentStep
    ? haversine(position, { lat: currentStep.lat, lng: currentStep.lng })
    : 0;
  const chat = chats[active?.id] || [];

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
  }, [plan.knoop_chain]);

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
    setPosition(here);
    if (fromGps) {
      setGps((current) => ({ ...here, accuracy: current?.accuracy || 25 }));
    }
    maybeAnnounce(here);
    advanceNav(here);
  }

  async function showMyLocation() {
    try {
      const next = await getBrowserLocation();
      setGps(next);
      setFollowGps(true);
      setRerouteError("");
    } catch (err) {
      setRerouteError(err.message);
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

  async function submitQuestion(event) {
    event.preventDefault();
    if (!active || question.trim().length < 2) return;
    setAskBusy(true);
    setAskError("");
    try {
      const data = await askAbout({
        question: question.trim(),
        name: active.name,
        kind: active.kind,
        summary: active.summary,
        arrived: active.arrived,
        explanation_level: plan.explanation_level || "normaal",
      });
      setChats((current) => ({
        ...current,
        [active.id]: [...(current[active.id] || []), { q: question.trim(), a: data.answer }],
      }));
      setQuestion("");
      speak(data.answer);
    } catch (err) {
      setAskError(err.message);
    } finally {
      setAskBusy(false);
    }
  }

  function nodeVariant(node) {
    const id = nodeId(node);
    if (customIds.includes(id) && !node.on_route) return "picked";
    if (customIds.includes(id)) return "route";
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
        {plan.ai_used && <div className="ai-pill">Gemini gids · {plan.explanation_level || "normaal"}</div>}
        {plan.route_reason && <p className="sources">{plan.route_reason}</p>}
        {plan.knoop_chain && (
          <div className="chain">
            {plan.knooppunten.map((node, index) => (
              <span key={`${nodeId(node)}-${index}`} style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                {index > 0 && <span className="node-arrow">→</span>}
                <span className="node">{node.number}</span>
              </span>
            ))}
          </div>
        )}
        <div className="stats">
          <div className="stat">
            <span>Afstand</span>
            <b>{plan.distance_km} km</b>
          </div>
          <div className="stat">
            <span>Rijtijd</span>
            <b>{plan.duration_min} min</b>
          </div>
          <div className="stat">
            <span>Knooppunten</span>
            <b>{allNodes.length}</b>
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
            Grijze nummers liggen in de buurt. Klik om ze toe te voegen of te schrappen, daarna
            herberekenen.
          </p>
          <div className="chain">
            {customIds.map((id, index) => {
              const node = nodeLookup.get(id);
              if (!node) return null;
              return (
                <button
                  key={id}
                  type="button"
                  className={`node ${node.on_route ? "" : "picked"}`}
                  onClick={() => toggleNode(node)}
                  title="Verwijder uit je route"
                >
                  {index > 0 ? `${node.number}` : node.number}
                </button>
              );
            })}
          </div>
          {dirty && (
            <button className="submit" type="button" onClick={applyCustomRoute} disabled={rerouteBusy}>
              {rerouteBusy ? "Route wordt herberekend..." : "Herbereken via deze knooppunten"}
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
        {active && (
          <form className="ask" onSubmit={submitQuestion}>
            <strong>Vraag over {active.name}</strong>
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
              placeholder="Bijvoorbeeld: wanneer is het gebouwd? mag ik naar binnen?"
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
            />
            {askError && <div className="error">{askError}</div>}
            <button className="submit" type="submit" disabled={askBusy || question.trim().length < 2}>
              {askBusy ? "De gids denkt na..." : "Stel bijvraag"}
            </button>
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
          <Polyline positions={plan.geometry} pathOptions={{ color: "#d0122d", weight: 5, opacity: 0.92 }} />
          {allNodes.map((node) => {
            const variant = nodeVariant(node);
            return (
              <Marker
                key={nodeId(node)}
                position={[node.lat, node.lng]}
                icon={nodeIcon(node.number, variant)}
                zIndexOffset={variant === "idle" ? 0 : 400}
                eventHandlers={{ click: () => toggleNode(node) }}
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
          <FitRoute geometry={plan.geometry} nodes={allNodes} />
          <Follow position={mode === "demo" ? position : gps || position} enabled={mode === "demo" || followGps} />
        </MapContainer>
        <button className="locate-fab" type="button" onClick={showMyLocation} title="Toon mijn locatie">
          ⌖
        </button>
        {currentStep && mode !== "idle" && (
          <div className="nav-banner">
            <div className="kicker">Volgende manoeuvre · {formatDistance(stepDistance)}</div>
            <p>{currentStep.instruction}</p>
          </div>
        )}
        <div className="guide">
          <div className="kicker">
            {phase === "intro" ? "Je gids" : active ? `${active.name} · ${active.kind}` : "Je gids"}
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
