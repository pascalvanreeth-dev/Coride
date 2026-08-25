import { useEffect, useMemo, useRef, useState } from "react";
import { MapContainer, Marker, Popup, TileLayer, useMap, useMapEvents } from "react-leaflet";
import L from "leaflet";
import { fetchKnooppunten, fetchRoutePreview, fetchRouteSuggestions, reverseGeocode, reroute } from "../api.js";
import { estimateRouteKm, formatKm, getBrowserLocation, nodeId } from "../geo.js";
import { useDebounced } from "../hooks.js";
import { nodeIcon, startIcon } from "../icons.js";
import { profileSummary, suggestedDistance, suggestedMinutes, toApiProfile, mergeInterests, interestLabels } from "../profile.js";
import { getUsedRouteIds } from "../routeHistory.js";
import HereMarker from "./HereMarker.jsx";
import MapFlyTo from "./MapFlyTo.jsx";
import LocateFab from "./LocateFab.jsx";
import MapResize from "./MapResize.jsx";
import RouteLine from "./RouteLine.jsx";
import "leaflet/dist/leaflet.css";

const COORD_QUERY = /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/;

export default function Planner({ busy, error, center, profile, onEditProfile, onPreview, onPlan, geocode }) {
  const [start, setStart] = useState("Gent");
  const [end, setEnd] = useState("");
  const [mode, setMode] = useState("lus");
  const [interests, setInterests] = useState(() =>
    profile?.interests?.length ? profile.interests : ["geschiedenis"],
  );
  const [distance, setDistance] = useState(() => suggestedDistance(profile));
  const [duration, setDuration] = useState(() => suggestedMinutes(profile));
  const [budgetMode, setBudgetMode] = useState("distance");
  const [notes, setNotes] = useState("");
  const [buildMode, setBuildMode] = useState("manual");
  const [hits, setHits] = useState([]);
  const [here, setHere] = useState(null);
  const [origin, setOrigin] = useState(null);
  const originRef = useRef(null);
  const [geoBusy, setGeoBusy] = useState(false);
  const [geoError, setGeoError] = useState("");
  const [locateTick, setLocateTick] = useState(0);
  const [nodes, setNodes] = useState([]);
  const [nodesBusy, setNodesBusy] = useState(false);
  const [selectedIds, setSelectedIds] = useState([]);
  const [draft, setDraft] = useState(null);
  const [draftBusy, setDraftBusy] = useState(false);
  const [suggestions, setSuggestions] = useState([]);
  const [suggestionsBusy, setSuggestionsBusy] = useState(false);
  const [selectedSuggestionId, setSelectedSuggestionId] = useState("");
  const [suggestionPreview, setSuggestionPreview] = useState(null);
  const [suggestionPreviewBusy, setSuggestionPreviewBusy] = useState(false);
  originRef.current = origin;
  const skipGeocode = useRef(false);
  const reverseKeyRef = useRef("");
  const query = useDebounced(start, 350);
  const selectedKey = useDebounced(selectedIds.join("|"), 450);
  const previewKey = useDebounced(
    origin &&
      ((buildMode === "suggest" && selectedSuggestionId) || buildMode === "auto")
      ? buildMode === "suggest"
        ? `${selectedSuggestionId}|${distance}|${origin.lat}|${origin.lng}|${mode}|${notes}`
        : `${distance}|${duration}|${budgetMode}|${origin.lat}|${origin.lng}|${mode}|${notes}`
      : "",
    450,
  );
  const previewDistanceKm =
    buildMode === "auto" && budgetMode === "time"
      ? Math.min(90, Math.max(8, Math.round((Number(duration) / 60) * 16)))
      : Number(distance);

  const nodeLookup = useMemo(() => {
    const map = new Map();
    for (const node of nodes) map.set(nodeId(node), node);
    return map;
  }, [nodes]);

  const selectedNodes = useMemo(
    () => selectedIds.map((id) => nodeLookup.get(id)).filter(Boolean),
    [nodeLookup, selectedIds],
  );

  const estimateKm = estimateRouteKm(origin, selectedNodes, mode !== "punt");
  const liveKm = draft?.distance_km ?? estimateKm;
  const liveMin = draft?.duration_min;

  useEffect(() => {
    if (skipGeocode.current) {
      skipGeocode.current = false;
      setHits([]);
      return undefined;
    }
    if (query.length < 3 || COORD_QUERY.test(query)) {
      setHits([]);
      return undefined;
    }
    geocode(query).then(setHits).catch(() => setHits([]));
    return undefined;
  }, [query, geocode]);

  useEffect(() => {
    let cancelled = false;
    getBrowserLocation()
      .then(async (next) => {
        if (cancelled) return;
        setHere(next);
        if (originRef.current?.source === "map" || originRef.current?.source === "route") return;
        await setFromCoords(next, "gps");
      })
      .catch(async () => {
        if (cancelled || originRef.current) return;
        try {
          const results = await geocode("Gent");
          const hit = results[0];
          if (!hit) return;
          setOrigin({ lat: hit.lat, lng: hit.lng, source: "search" });
          skipGeocode.current = true;
          setStart(hit.label);
          onPreview({ lat: hit.lat, lng: hit.lng, zoom: 12 });
        } catch {
          /* geen fallback */
        }
      });
    return () => {
      cancelled = true;
    };
  }, [geocode, onPreview]);

  useEffect(() => {
    if (!origin || buildMode !== "manual") return undefined;
    let cancelled = false;
    setNodesBusy(true);
    fetchKnooppunten(origin.lat, origin.lng)
      .then((next) => {
        if (cancelled) return;
        setNodes(next);
        setSelectedIds((current) => current.filter((id) => next.some((node) => nodeId(node) === id)));
      })
      .catch((err) => {
        if (!cancelled) setGeoError(err.message);
      })
      .finally(() => {
        if (!cancelled) setNodesBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [buildMode, origin?.lat, origin?.lng]);

  useEffect(() => {
    if (buildMode !== "manual" || !origin || !selectedKey) {
      setDraft(null);
      setDraftBusy(false);
      return undefined;
    }
    const picked = selectedKey
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
      start_lat: origin.lat,
      start_lng: origin.lng,
      nodes: picked,
      close_loop: mode !== "punt",
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
  }, [buildMode, mode, nodeLookup, origin, selectedKey]);

  useEffect(() => {
    if (profile?.interests?.length) setInterests(profile.interests);
  }, [profile]);

  const activeInterests = profile?.interests?.length ? profile.interests : interests;

  useEffect(() => {
    if (buildMode !== "suggest") return undefined;
    const lat = origin?.lat ?? here?.lat ?? 51.05;
    const lng = origin?.lng ?? here?.lng ?? 3.72;
    let cancelled = false;
    setSuggestionsBusy(true);
    fetchRouteSuggestions(lat, lng, activeInterests, getUsedRouteIds())
      .then((next) => {
        if (cancelled) return;
        setSuggestions(next);
        setSelectedSuggestionId((current) => (current && next.some((item) => item.id === current) ? current : ""));
      })
      .catch((err) => {
        if (!cancelled) setGeoError(err.message);
      })
      .finally(() => {
        if (!cancelled) setSuggestionsBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [buildMode, origin?.lat, origin?.lng, here?.lat, here?.lng, activeInterests.join("|")]);

  useEffect(() => {
    if ((buildMode !== "suggest" && buildMode !== "auto") || !previewKey || !origin) {
      setSuggestionPreview(null);
      setSuggestionPreviewBusy(false);
      return undefined;
    }
    let cancelled = false;
    setSuggestionPreviewBusy(true);
    fetchRoutePreview({
      lat: origin.lat,
      lng: origin.lng,
      distance_km: previewDistanceKm,
      mode,
      notes,
    })
      .then((next) => {
        if (!cancelled) setSuggestionPreview(next);
      })
      .catch(() => {
        if (!cancelled) setSuggestionPreview(null);
      })
      .finally(() => {
        if (!cancelled) setSuggestionPreviewBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [buildMode, previewKey, origin, previewDistanceKm, mode, notes]);

  const routePreview = suggestionPreview;
  const routePreviewBusy = suggestionPreviewBusy;

  const selectedSuggestion = useMemo(
    () => suggestions.find((item) => item.id === selectedSuggestionId) || null,
    [suggestions, selectedSuggestionId],
  );

  const suggestInterests = useMemo(
    () =>
      buildMode === "suggest" && selectedSuggestion
        ? mergeInterests(selectedSuggestion.interests, activeInterests)
        : activeInterests,
    [buildMode, selectedSuggestion, activeInterests],
  );

  function applySuggestion(suggestion) {
    if (!Number.isFinite(suggestion.lat) || !Number.isFinite(suggestion.lng)) {
      setGeoError("Deze route heeft geen geldig startpunt. Probeer opnieuw te laden.");
      return;
    }
    setSelectedSuggestionId(suggestion.id);
    setStart(suggestion.start);
    setEnd(suggestion.end || "");
    setMode(suggestion.mode);
    setInterests(suggestion.interests);
    setDistance(suggestion.distance_km);
    setDuration(Math.round((suggestion.distance_km / 16) * 60));
    setBudgetMode("distance");
    setNotes(suggestion.notes || "");
    setSelectedIds([]);
    setSuggestionPreview(null);
    setGeoError("");
    const startPoint = { lat: suggestion.lat, lng: suggestion.lng };
    setHere(startPoint);
    setOrigin({ lat: suggestion.lat, lng: suggestion.lng, source: "route" });
    setLocateTick((tick) => tick + 1);
    onPreview({ lat: suggestion.lat, lng: suggestion.lng, zoom: 11 });
    skipGeocode.current = true;
  }

  async function setFromCoords(next, source, label) {
    setOrigin({ lat: next.lat, lng: next.lng, source });
    onPreview({ lat: next.lat, lng: next.lng, zoom: 14 });
    skipGeocode.current = true;
    if (label) {
      setStart(label);
      return;
    }
    const key = `${next.lat.toFixed(4)},${next.lng.toFixed(4)}`;
    if (reverseKeyRef.current === key) return;
    reverseKeyRef.current = key;
    try {
      const hit = await reverseGeocode(next.lat, next.lng);
      skipGeocode.current = true;
      setStart(hit.label);
    } catch {
      skipGeocode.current = true;
      setStart(`${next.lat.toFixed(5)}, ${next.lng.toFixed(5)}`);
    }
  }

  async function useMyLocation() {
    setGeoBusy(true);
    setGeoError("");
    try {
      const next = await getBrowserLocation();
      setHere(next);
      await setFromCoords(next, "gps");
      setLocateTick((tick) => tick + 1);
    } catch (err) {
      setGeoError(err.message);
    } finally {
      setGeoBusy(false);
    }
  }

  async function pickOnMap(next) {
    setGeoError("");
    await setFromCoords(next, "map");
  }

  function toggleNode(node) {
    if (buildMode !== "manual") return;
    const id = nodeId(node);
    setSelectedIds((current) => {
      if (current.includes(id)) return current.filter((item) => item !== id);
      return [...current, id];
    });
  }

  function nodeVariant(node) {
    return selectedIds.includes(nodeId(node)) ? "picked" : "idle";
  }

  function submit(event) {
    event.preventDefault();
    if (buildMode === "manual" && !selectedNodes.length) {
      setGeoError("Kies minstens één knooppunt op de kaart, of plan je tocht via ‘Plan mijn tocht’.");
      return;
    }
    if (buildMode === "suggest" && !selectedSuggestion) {
      setGeoError("Kies eerst een route uit de Top 10.");
      return;
    }
    const tripInterests =
      buildMode === "suggest" && selectedSuggestion
        ? mergeInterests(selectedSuggestion.interests, activeInterests)
        : activeInterests.length
          ? activeInterests
          : ["geschiedenis"];
    const distanceKm =
      buildMode === "manual"
        ? Math.min(90, Math.max(8, Math.round(liveKm || distance)))
        : buildMode === "suggest" && selectedSuggestion
          ? Number(distance)
          : budgetMode === "time"
            ? Math.min(90, Math.max(8, Math.round((Number(duration) / 60) * 16)))
            : Number(distance);
    onPlan({
      start: origin ? `${origin.lat.toFixed(5)}, ${origin.lng.toFixed(5)}` : start,
      end: mode === "punt" ? end : null,
      mode,
      interests: tripInterests.length ? tripInterests : ["geschiedenis"],
      distance_km: distanceKm,
      duration_min: budgetMode === "time" && buildMode === "auto" ? Number(duration) : null,
      budget_mode: budgetMode,
      notes: buildMode === "auto" || buildMode === "suggest" ? notes : "",
      explanation_level: profile?.commentary || "normaal",
      profile: toApiProfile(profile),
      suggestion_id: buildMode === "suggest" ? selectedSuggestionId : null,
      knooppunten:
        buildMode === "manual"
          ? selectedNodes.map((node) => ({
              id: node.id || "",
              number: node.number,
              lat: node.lat,
              lng: node.lng,
              network: node.network || null,
            }))
          : [],
    });
  }

  return (
    <div className="planner">
      <section className="panel">
        <form id="plan-form" className="panel-scroll" onSubmit={submit}>
        <div className="panel-intro">
          <div className="eyebrow">Vlaanderen · knooppunten</div>
          <h1 className="brand">Veloverhaal</h1>
          <p className="lede">
            Kies knooppunten, plan zelf, of start met een route uit de Top 10.
          </p>
        </div>

        <div className="profile-bar">
          <span>{profileSummary(profile)}</span>
          <button type="button" className="ghost-link" onClick={onEditProfile}>
            Profiel aanpassen
          </button>
        </div>

        <div className="choice">
          <button
            type="button"
            className={`choice-card ${buildMode === "manual" ? "on" : ""}`}
            onClick={() => {
              setGeoError("");
              setSelectedSuggestionId("");
              setSuggestionPreview(null);
              setBuildMode("manual");
            }}
          >
            <strong>Zelf knooppunten kiezen</strong>
            <span>Klik de nummers op de kaart. Je ziet meteen hoeveel kilometer de route al is.</span>
          </button>
          <button
            type="button"
            className={`choice-card ${buildMode === "auto" ? "on" : ""}`}
            onClick={() => {
              setGeoError("");
              setSelectedSuggestionId("");
              setSuggestionPreview(null);
              setBuildMode("auto");
            }}
          >
            <strong>Plan mijn tocht</strong>
            <span>Geef afstand of tijd. De gids kiest knooppunten en plekken op basis van je profiel.</span>
          </button>
          <button
            type="button"
            className={`choice-card ${buildMode === "suggest" ? "on" : ""}`}
            onClick={() => {
              setGeoError("");
              setBuildMode("suggest");
            }}
          >
            <strong>Route Top 10</strong>
            <span>Tien kant-en-klare tochten (~50 km) rond Vlaamse steden en bezienswaardigheden.</span>
          </button>
        </div>

          {buildMode === "manual" && (
          <>
          <label>
            Startlocatie
            <div className="start-row">
              <input
                value={start}
                onChange={(event) => {
                  setStart(event.target.value);
                }}
                required
              />
              <button type="button" className="locate-chip" onClick={useMyLocation} disabled={geoBusy}>
                {geoBusy ? "GPS..." : "Mijn locatie"}
              </button>
            </div>
          </label>
          {geoError && <div className="error">{geoError}</div>}
          {hits.length > 0 && (
            <ul className="suggest">
              {hits.slice(0, 5).map((hit) => (
                <li key={hit.label}>
                  <button
                    type="button"
                    onClick={() => {
                      setHits([]);
                      setFromCoords(hit, "search", hit.label);
                    }}
                  >
                    {hit.label}
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="row">
            <button type="button" className={`mode ${mode === "lus" ? "on" : ""}`} onClick={() => setMode("lus")}>
              Lus
            </button>
            <button type="button" className={`mode ${mode === "punt" ? "on" : ""}`} onClick={() => setMode("punt")}>
              Van A naar B
            </button>
          </div>

          {mode === "punt" && (
            <label>
              Eindlocatie
              <input value={end} onChange={(event) => setEnd(event.target.value)} required={mode === "punt"} />
            </label>
          )}
          </>
          )}

          {buildMode === "auto" && (
          <>
          <label>
            Startlocatie
            <div className="start-row">
              <input
                value={start}
                onChange={(event) => {
                  setStart(event.target.value);
                }}
                required
              />
              <button type="button" className="locate-chip" onClick={useMyLocation} disabled={geoBusy}>
                {geoBusy ? "GPS..." : "Mijn locatie"}
              </button>
            </div>
          </label>
          {geoError && <div className="error">{geoError}</div>}
          {hits.length > 0 && (
            <ul className="suggest">
              {hits.slice(0, 5).map((hit) => (
                <li key={hit.label}>
                  <button
                    type="button"
                    onClick={() => {
                      setHits([]);
                      setFromCoords(hit, "search", hit.label);
                    }}
                  >
                    {hit.label}
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="row">
            <button type="button" className={`mode ${mode === "lus" ? "on" : ""}`} onClick={() => setMode("lus")}>
              Lus
            </button>
            <button type="button" className={`mode ${mode === "punt" ? "on" : ""}`} onClick={() => setMode("punt")}>
              Van A naar B
            </button>
          </div>

          {mode === "punt" && (
            <label>
              Eindlocatie
              <input value={end} onChange={(event) => setEnd(event.target.value)} required={mode === "punt"} />
            </label>
          )}
          </>
          )}

          {geoError && buildMode === "suggest" && <div className="error">{geoError}</div>}

          {buildMode === "suggest" && (
            <div className="editor suggest-routes">
              <strong>Route Top 10</strong>
              <p className="sources" style={{ margin: "6px 0 10px" }}>
                {suggestionsBusy
                  ? "Routes worden geladen..."
                  : "Kant-en-klare tochten van ongeveer 50 km. Pas de lengte nadien aan."}
              </p>
              <div className="suggest-route-list">
                {suggestions.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className={`suggest-route ${selectedSuggestionId === item.id ? "on" : ""}`}
                    onClick={() => applySuggestion(item)}
                  >
                    <span className="suggest-route-main">
                      <strong>
                        <span className="suggest-route-rank">{item.rank}.</span> {item.title}
                      </strong>
                      <span>
                        {item.highlight} · ca. {item.distance_km} km
                        {item.distance_from_you_km != null ? ` · ${item.distance_from_you_km} km van jou` : ""}
                        {item.municipalities?.length ? ` · ${item.municipalities.slice(0, 3).join(", ")}` : ""}
                      </span>
                      {item.match_score > 0 && (
                        <small className="suggest-route-note">Past bij {item.match_score} van je interesses</small>
                      )}
                    </span>
                    {item.used_before && (
                      <small className="suggest-route-note">Eerder gefietst</small>
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}

          {buildMode === "suggest" && selectedSuggestion && (
            <div className="editor suggest-detail">
              <p className="sources" style={{ margin: "0 0 8px" }}>
                <strong>Start:</strong> {selectedSuggestion.start}
              </p>
              <p className="sources" style={{ margin: "0 0 8px" }}>
                <strong>Gemeenten:</strong> {(selectedSuggestion.municipalities || []).join(" · ")}
              </p>
              <div className="guide-pill-row">
                {interestLabels(suggestInterests).map((label) => (
                  <span key={label} className="guide-pill">
                    {label}
                  </span>
                ))}
              </div>
              <p className="sources" style={{ margin: "8px 0 0" }}>
                Route-thema's worden gecombineerd met je profiel. Tijdens de rit krijg je meldingen per
                gemeente en bezienswaardigheid.
              </p>
            </div>
          )}

          {buildMode === "suggest" && selectedSuggestion && (
            <label>
              <span className="range">
                Lengte van het traject <b>{distance} km</b>
                {suggestionPreview?.distance_km
                  ? ` · voorbeeld ${suggestionPreview.distance_km} km`
                  : suggestionPreviewBusy
                    ? " · route wordt getekend..."
                    : ""}
              </span>
              <input
                type="range"
                min="35"
                max="70"
                value={distance}
                onChange={(event) => setDistance(Number(event.target.value))}
              />
            </label>
          )}

          {buildMode === "suggest" && selectedSuggestion && suggestionPreview?.knooppunten?.length > 0 && (
            <div className="editor draft-box">
              <strong>Te volgen knooppunten</strong>
              <p className="sources" style={{ margin: "6px 0 8px" }}>
                {suggestionPreviewBusy
                  ? "Knooppuntenroute wordt berekend..."
                  : `${suggestionPreview.knooppunten.length} knooppunten in volgorde`}
              </p>
              <ol className="picked-list route-knoop-list">
                {suggestionPreview.knooppunten.map((node, index) => (
                  <li key={`${node.id || node.number}-${index}`}>
                    <span className="num">{index + 1}</span>
                    <span>
                      <strong>Knooppunt {node.number}</strong>
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          )}

          {buildMode === "manual" && (
          <div className="editor draft-box">
            <strong>Geselecteerde knooppunten</strong>
            <p className="sources" style={{ margin: "6px 0 8px" }}>
              {nodesBusy
                ? "Knooppunten worden geladen..."
                : nodes.length
                  ? "Klik op de rode nummers op de kaart. Nog eens klikken schrapt ze."
                  : "Klik op de kaart of kies ‘Mijn locatie’ om knooppunten te zien."}
            </p>
            {selectedNodes.length ? (
              <ol className="picked-list">
                {selectedNodes.map((node, index) => (
                  <li key={nodeId(node)}>
                    <span className="num">{index + 1}</span>
                    <span>
                      <strong>Knooppunt {node.number}</strong>
                      {index < selectedNodes.length - 1 && (
                        <small> daarna {selectedNodes[index + 1].number}</small>
                      )}
                    </span>
                    <button type="button" className="ghost-mini" onClick={() => toggleNode(node)}>
                      ×
                    </button>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="sources" style={{ margin: 0 }}>
                Nog geen knooppunten gekozen.
              </p>
            )}
            <div className="stats">
              <div className="stat">
                <span>Afstand</span>
                <b>{selectedNodes.length ? formatKm(liveKm) : "0 km"}</b>
              </div>
              <div className="stat">
                <span>Rijtijd</span>
                <b>{liveMin ? `${liveMin} min` : selectedNodes.length ? "…" : "—"}</b>
              </div>
              <div className="stat">
                <span>Gekozen</span>
                <b>{selectedNodes.length}</b>
              </div>
            </div>
            {draftBusy && selectedNodes.length > 0 && (
              <p className="sources" style={{ margin: 0 }}>
                Fietsroute wordt herberekend…
              </p>
            )}
            {draft?.knooppunten?.length > 1 && (
              <>
                <strong style={{ display: "block", marginTop: 12 }}>Te volgen knooppunten</strong>
                <p className="sources" style={{ margin: "6px 0 8px" }}>
                  Volledige route via het officiële knooppuntennetwerk.
                </p>
                <ol className="picked-list route-knoop-list">
                  {draft.knooppunten.map((node, index) => (
                    <li key={`${node.id || node.number}-${index}`}>
                      <span className="num">{index + 1}</span>
                      <span>
                        <strong>Knooppunt {node.number}</strong>
                      </span>
                    </li>
                  ))}
                </ol>
              </>
            )}
            {selectedIds.length > 0 && (
              <button type="button" className="ghost-link" onClick={() => setSelectedIds([])}>
                Selectie wissen
              </button>
            )}
          </div>
          )}

          {buildMode === "auto" && mode === "lus" && (
            <>
              <div className="row">
                <button
                  type="button"
                  className={`mode ${budgetMode === "distance" ? "on" : ""}`}
                  onClick={() => setBudgetMode("distance")}
                >
                  Kilometers
                </button>
                <button
                  type="button"
                  className={`mode ${budgetMode === "time" ? "on" : ""}`}
                  onClick={() => setBudgetMode("time")}
                >
                  Tijd
                </button>
              </div>
              {budgetMode === "distance" ? (
                <label>
                  <span className="range">
                    Hoeveel kilometer? <b>{distance} km</b>
                  </span>
                  <input
                    type="range"
                    min="10"
                    max="80"
                    value={distance}
                    onChange={(event) => setDistance(event.target.value)}
                  />
                </label>
              ) : (
                <label>
                  <span className="range">
                    Hoeveel tijd? <b>{duration} min</b>
                  </span>
                  <input
                    type="range"
                    min="30"
                    max="300"
                    step="15"
                    value={duration}
                    onChange={(event) => setDuration(event.target.value)}
                  />
                </label>
              )}
              <p className="sources" style={{ margin: 0 }}>
                We starten vanaf je huidige locatie (of het gekozen startpunt) via officiële
                fietsknooppunten.
              </p>
              {routePreview?.knooppunten?.length > 0 && (
                <>
                  <strong style={{ display: "block", marginTop: 12 }}>Te volgen knooppunten</strong>
                  <p className="sources" style={{ margin: "6px 0 8px" }}>
                    {routePreviewBusy
                      ? "Knooppuntenroute wordt berekend..."
                      : `${routePreview.knooppunten.length} knooppunten · voorbeeld ${routePreview.distance_km} km`}
                  </p>
                  <ol className="picked-list route-knoop-list">
                    {routePreview.knooppunten.map((node, index) => (
                      <li key={`${node.id || node.number}-${index}`}>
                        <span className="num">{index + 1}</span>
                        <span>
                          <strong>Knooppunt {node.number}</strong>
                        </span>
                      </li>
                    ))}
                  </ol>
                </>
              )}
            </>
          )}

          {buildMode === "auto" && (
          <label>
            Extra wens — we passen de knooppunten hierop aan
            <textarea
              rows="2"
              placeholder="Bijvoorbeeld: cafés, kastelen, langs het water..."
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
            />
          </label>
          )}

          {buildMode === "suggest" && selectedSuggestion && (
          <label>
            Toelichting bij deze route
            <textarea
              rows="2"
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
            />
          </label>
          )}

        </form>

        <div className="panel-footer">
          {error && <div className="error">{error}</div>}

          <button
            className="submit"
            type="submit"
            form="plan-form"
            disabled={busy || (buildMode === "suggest" && !selectedSuggestion)}
          >
            {busy
              ? buildMode === "suggest"
                ? "Route wordt samengesteld..."
                : buildMode === "auto"
                  ? "Je tocht wordt samengesteld..."
                  : "Je knooppuntenroute wordt gepland..."
              : buildMode === "manual"
                ? "Plan deze knooppuntenroute"
                : buildMode === "suggest"
                  ? "Start deze route"
                  : "Plan mijn tocht"}
          </button>

          <p className="sources">
            Kaart: CyclOSM. Route: OSRM. Knooppunten: Toerisme Vlaanderen (gratis WFS). Uitleg:
            Wikipedia.
          </p>
        </div>
      </section>

      <section className="hero-map">
        <MapContainer center={center} zoom={8} attributionControl>
          <TileLayer
            attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://www.cyclosm.org">CyclOSM</a>'
            url="https://{s}.tile-cyclosm.openstreetmap.fr/cyclosm/{z}/{x}/{y}.png"
          />
          <MapClick onPick={pickOnMap} />
          <MapResize />
          {buildMode === "manual" && draft?.geometry?.length > 1 && (
            <RouteLine positions={draft.geometry} />
          )}
          {(buildMode === "suggest" || buildMode === "auto") && routePreview?.geometry?.length > 1 && (
            <RouteLine positions={routePreview.geometry} />
          )}
          {buildMode === "manual" &&
            nodes.map((node) => {
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
                  <p>
                    {selectedIds.includes(nodeId(node))
                      ? "Op je route. Klik om te schrappen."
                      : "Klik om langs hier te fietsen."}
                  </p>
                </Popup>
              </Marker>
            );
          })}
          {origin && origin.source !== "gps" && buildMode !== "suggest" && (
            <Marker position={[origin.lat, origin.lng]} icon={startIcon} zIndexOffset={1100}>
              <Popup>Startpunt</Popup>
            </Marker>
          )}
          <HereMarker position={here} accuracy={buildMode === "suggest" ? 0 : here?.accuracy} />
          <MapFlyTo
            position={buildMode === "suggest" ? origin : here}
            trigger={locateTick}
            zoom={buildMode === "suggest" ? 11 : 15}
          />
          <Recenter
            center={center}
            zoom={origin ? 13 : 12}
            locked={
              (buildMode === "manual" && selectedNodes.length > 0) ||
              (buildMode === "suggest" && !!selectedSuggestion)
            }
          />
          {buildMode === "manual" && <FitSelection origin={origin} nodes={selectedNodes} />}
          {buildMode === "manual" && nodes.length > 0 && selectedNodes.length === 0 && (
            <FitNodes origin={origin} nodes={nodes} />
          )}
          {(buildMode === "suggest" || buildMode === "auto") && (
            <FitPreview
              geometry={routePreview?.geometry}
              active={buildMode === "suggest" ? !!selectedSuggestion : !!origin}
            />
          )}
        </MapContainer>
        <div className="map-overlay">
          <LocateFab onClick={useMyLocation} disabled={geoBusy} busy={geoBusy} />
          <div className="map-hint">
          {buildMode === "manual"
            ? selectedNodes.length
              ? `${selectedNodes.length} knooppunten · ${formatKm(liveKm)}`
              : "Klik op de kaart voor je startpunt, daarna op de nummers"
            : buildMode === "suggest"
              ? selectedSuggestion
                ? routePreviewBusy
                  ? `${selectedSuggestion.title} · route laden...`
                  : `${selectedSuggestion.title} · ${distance} km`
                : "Kies een route uit de Top 10"
              : buildMode === "auto"
                ? routePreviewBusy
                  ? "Knooppuntenroute laden..."
                  : routePreview?.knooppunten?.length
                    ? `${routePreview.knooppunten.length} knooppunten · ${formatKm(routePreview.distance_km)}`
                    : "Klik op de kaart of kies ‘Mijn locatie’ voor je startpunt"
                : "Klik op de kaart of kies ‘Mijn locatie’ voor je startpunt"}
          </div>
        </div>
      </section>
    </div>
  );
}

function MapClick({ onPick }) {
  useMapEvents({
    click(event) {
      const target = event.originalEvent?.target;
      if (target?.closest?.(".leaflet-marker-icon, .leaflet-popup, .leaflet-control")) return;
      onPick({ lat: event.latlng.lat, lng: event.latlng.lng });
    },
  });
  return null;
}

function Recenter({ center, zoom, locked }) {
  const map = useMap();
  useEffect(() => {
    if (locked) return;
    map.setView(center, zoom || map.getZoom());
  }, [center, locked, map, zoom]);
  return null;
}

function FitSelection({ origin, nodes }) {
  const map = useMap();
  const key = `${origin?.lat},${origin?.lng}|${nodes.map((node) => nodeId(node)).join("|")}`;
  useEffect(() => {
    const points = [];
    if (origin) points.push([origin.lat, origin.lng]);
    for (const node of nodes || []) points.push([node.lat, node.lng]);
    if (points.length < 2) return;
    map.fitBounds(points, { padding: [72, 72], maxZoom: 14 });
  }, [key, map]);
  return null;
}

function FitNodes({ origin, nodes }) {
  const map = useMap();
  const key = `${origin?.lat},${origin?.lng}|${nodes.length}`;
  useEffect(() => {
    const points = [];
    if (origin) points.push([origin.lat, origin.lng]);
    for (const node of nodes.slice(0, 48)) points.push([node.lat, node.lng]);
    if (points.length < 2) return;
    map.fitBounds(points, { padding: [84, 84], maxZoom: 13 });
  }, [key, map, nodes, origin]);
  return null;
}

function FitPreview({ geometry, active }) {
  const map = useMap();
  useEffect(() => {
    if (!active || !geometry?.length) return;
    map.fitBounds(geometry, { padding: [72, 72], maxZoom: 13 });
  }, [active, geometry, map]);
  return null;
}
