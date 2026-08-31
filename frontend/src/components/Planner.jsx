import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MapContainer, Marker, Popup, TileLayer, useMap, useMapEvents } from "react-leaflet";
import L from "leaflet";
import { fetchKnooppunten, fetchRoutePreview, fetchRouteSuggestions, reverseGeocode, reroute } from "../api.js";
import { estimateRouteKm, formatDuration, formatKm, getBrowserLocation, displayLoopNodes, ID_JOIN, knoopMatches, knoopOnRoute, knoopOnGeometry, mergeMapKnooppunten, nodeId } from "../geo.js";
import { useDebounced } from "../hooks.js";
import { nodeIcon, startIcon, wishPoiIcon } from "../icons.js";
import { profileSummary, suggestedDistance, suggestedMinutes, toApiProfile, mergeInterests, interestLabels } from "../profile.js";
import { MAP_SOURCES, MAP_TILE } from "../mapTiles.js";
import { getUsedRouteIds } from "../routeHistory.js";
import HereMarker from "./HereMarker.jsx";
import MapChrome from "./MapChrome.jsx";
import MapFlyTo from "./MapFlyTo.jsx";
import MapReady from "./MapReady.jsx";
import MapResize from "./MapResize.jsx";
import MapZoomScale, { useMapZoom } from "./MapZoomScale.jsx";
import RouteLine from "./RouteLine.jsx";
import "leaflet/dist/leaflet.css";

const COORD_QUERY = /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/;

export default function Planner({ busy, error, center, zoom = 14, profile, onEditProfile, onPreview, onPlan }) {
  const [map, setMap] = useState(null);
  const [start, setStart] = useState("");
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
  const [startChoice, setStartChoice] = useState(null); // "map" | "gps"
  const [here, setHere] = useState(null);
  const [origin, setOrigin] = useState(null);
  const originRef = useRef(null);
  const [geoBusy, setGeoBusy] = useState(false);
  const [geoError, setGeoError] = useState("");
  const [locateTick, setLocateTick] = useState(0);
  const [nodes, setNodes] = useState([]);
  const [nodeCatalog, setNodeCatalog] = useState({});
  const [viewFocus, setViewFocus] = useState(null);
  const [nodesBusy, setNodesBusy] = useState(false);
  const [selectedIds, setSelectedIds] = useState([]);
  const [draft, setDraft] = useState(null);
  const [draftBusy, setDraftBusy] = useState(false);
  const [suggestions, setSuggestions] = useState([]);
  const [suggestionsBusy, setSuggestionsBusy] = useState(false);
  const [selectedSuggestionId, setSelectedSuggestionId] = useState("");
  const [suggestionPreview, setSuggestionPreview] = useState(null);
  const [suggestionPreviewBusy, setSuggestionPreviewBusy] = useState(false);
  const [modePickerOpen, setModePickerOpen] = useState(false);
  const [startPickerOpen, setStartPickerOpen] = useState(false);
  const [pendingStartChoice, setPendingStartChoice] = useState(null); // "map" | "gps"
  originRef.current = origin;
  const reverseKeyRef = useRef("");
  const selectedKey = useDebounced(selectedIds.join(ID_JOIN), 450);
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

  function rememberNodes(...items) {
    setNodeCatalog((current) => {
      const next = { ...current };
      for (const node of items) {
        if (!node) continue;
        next[nodeId(node)] = node;
      }
      return next;
    });
  }

  const nodeLookup = useMemo(() => {
    const map = new Map();
    for (const node of Object.values(nodeCatalog)) map.set(nodeId(node), node);
    for (const node of nodes) map.set(nodeId(node), node);
    for (const node of draft?.knooppunten || []) map.set(nodeId(node), node);
    return map;
  }, [nodeCatalog, nodes, draft]);

  const selectedNodes = useMemo(
    () => selectedIds.map((id) => nodeLookup.get(id)).filter(Boolean),
    [nodeLookup, selectedIds],
  );

  const routeNodes = useMemo(() => {
    const base = draft?.knooppunten?.length ? draft.knooppunten : selectedNodes;
    return displayLoopNodes(base, mode !== "punt");
  }, [draft, selectedNodes, mode]);

  const selectedIdSet = useMemo(() => new Set(selectedIds), [selectedIds]);

  const mapNodes = useMemo(
    () =>
      mergeMapKnooppunten(
        nodes,
        routeNodes,
        selectedNodes,
        draft?.geometry || suggestionPreview?.geometry,
      ),
    [nodes, routeNodes, selectedNodes, draft?.geometry, suggestionPreview?.geometry],
  );

  const estimateKm = estimateRouteKm(origin, selectedNodes, mode !== "punt");
  const liveKm = draft?.distance_km ?? estimateKm;
  const liveMin = draft?.duration_min;

  // Kaart centreren op GPS bij start; origin pas na expliciete startkeuze.
  useEffect(() => {
    let cancelled = false;
    getBrowserLocation()
      .then((next) => {
        if (cancelled) return;
        setHere(next);
        if (!originRef.current) {
          onPreview({ lat: next.lat, lng: next.lng, zoom: 14 });
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [onPreview]);

  const lastSelectedNode = useMemo(
    () => (selectedNodes.length ? selectedNodes[selectedNodes.length - 1] : null),
    [selectedNodes],
  );

  const panFocusActive =
    startChoice === "map" &&
    (buildMode === "manual" || buildMode === "auto") &&
    selectedIds.length === 0;

  const nodeFocus = useMemo(() => {
    if ((buildMode === "manual" || buildMode === "auto") && lastSelectedNode) {
      return { lat: lastSelectedNode.lat, lng: lastSelectedNode.lng };
    }
    if (panFocusActive && viewFocus) {
      return viewFocus;
    }
    if (origin) return { lat: origin.lat, lng: origin.lng };
    if (startChoice === "map" && center?.[0] != null) {
      return { lat: center[0], lng: center[1] };
    }
    if (here) return { lat: here.lat, lng: here.lng };
    return null;
  }, [buildMode, lastSelectedNode, panFocusActive, viewFocus, origin, startChoice, center, here]);

  useEffect(() => {
    if ((buildMode !== "manual" && buildMode !== "auto") || !nodeFocus) return undefined;
    let cancelled = false;
    setNodesBusy(true);
    fetchKnooppunten(nodeFocus.lat, nodeFocus.lng)
      .then((next) => {
        if (cancelled) return;
        setNodes(next);
        rememberNodes(...next);
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
  }, [buildMode, nodeFocus?.lat, nodeFocus?.lng]);

  useEffect(() => {
    if (buildMode !== "manual" || !selectedKey) {
      setDraft(null);
      setDraftBusy(false);
      return undefined;
    }
    const picked = selectedKey
      .split(ID_JOIN)
      .filter(Boolean)
      .map((id) => nodeLookup.get(id))
      .filter(Boolean);
    if (!picked.length) {
      setDraft(null);
      return undefined;
    }
    const startPoint = mode === "punt" ? picked[0] : origin;
    if (!startPoint) {
      setDraft(null);
      return undefined;
    }
    const endPoint = mode === "punt" && picked.length >= 2 ? picked[picked.length - 1] : null;
    let cancelled = false;
    setDraftBusy(true);
    reroute({
      start_lat: startPoint.lat,
      start_lng: startPoint.lng,
      nodes: picked.map((node) => ({
        id: node.id || "",
        number: node.number,
        lat: node.lat,
        lng: node.lng,
        network: node.network || null,
        geoid: node.geoid ?? null,
        on_route: true,
      })),
      close_loop: mode !== "punt",
      end_lat: endPoint?.lat ?? null,
      end_lng: endPoint?.lng ?? null,
      poi_picks: [],
    })
      .then((next) => {
        if (!cancelled) {
          setDraft(next);
          rememberNodes(...(next.knooppunten || []));
        }
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

  const previewKnooppunten = useMemo(
    () => displayLoopNodes(routePreview?.knooppunten || [], mode !== "punt"),
    [routePreview?.knooppunten, mode],
  );
  const suggestPreviewKnooppunten = useMemo(
    () =>
      displayLoopNodes(
        suggestionPreview?.knooppunten || [],
        (selectedSuggestion?.mode || mode) !== "punt",
      ),
    [suggestionPreview?.knooppunten, selectedSuggestion?.mode, mode],
  );

  const wishSuggestions = useMemo(() => {
    if (!notes.trim() || (buildMode !== "auto" && buildMode !== "suggest")) return [];
    return routePreview?.suggestions || [];
  }, [buildMode, notes, routePreview?.suggestions]);

  const wishOnRoute = useMemo(
    () => wishSuggestions.filter((item) => item.on_route),
    [wishSuggestions],
  );
  const wishOffRoute = useMemo(
    () => wishSuggestions.filter((item) => !item.on_route),
    [wishSuggestions],
  );
  const [focusedWishId, setFocusedWishId] = useState("");

  useEffect(() => {
    setFocusedWishId("");
  }, [notes, routePreview?.suggestions]);

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
    setStart(shortPlaceLabel(suggestion.start) || suggestion.start);
    setStartChoice("map");
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
  }

  async function setFromCoords(next, source, label) {
    setOrigin({ lat: next.lat, lng: next.lng, source });
    onPreview({ lat: next.lat, lng: next.lng, zoom: 14 });
    if (label) {
      setStart(shortPlaceLabel(label));
      return;
    }
    const key = `${next.lat.toFixed(4)},${next.lng.toFixed(4)}`;
    if (reverseKeyRef.current === key) return;
    reverseKeyRef.current = key;
    const fallback =
      source === "gps"
        ? "Jouw huidige locatie"
        : source === "knoop"
          ? "Startknooppunt"
          : source === "map"
            ? "Gekozen op de kaart"
            : "Startpunt gekozen";
    try {
      const hit = await reverseGeocode(next.lat, next.lng);
      const nice = hit?.label && !COORD_QUERY.test(hit.label) ? shortPlaceLabel(hit.label) : fallback;
      setStart(nice);
    } catch {
      setStart(fallback);
    }
  }

  function chooseMapStart() {
    setStartChoice("map");
    setGeoError("");
    setOrigin(null);
    setStart("");
    setSelectedIds([]);
    setNodeCatalog({});
    setViewFocus(null);
    setDraft(null);
    reverseKeyRef.current = "";
  }

  function requestMapStart() {
    setStartPickerOpen(false);
    if (buildMode === "manual") {
      setMode("punt");
      chooseMapStart();
      return;
    }
    setPendingStartChoice("map");
    setModePickerOpen(true);
  }

  function requestMyLocation() {
    setStartPickerOpen(false);
    if (buildMode === "manual") {
      setMode("punt");
      chooseMyLocation();
      return;
    }
    setPendingStartChoice("gps");
    setModePickerOpen(true);
  }

  function cancelStartPicker() {
    setStartPickerOpen(false);
  }

  function cancelRouteModePicker() {
    setModePickerOpen(false);
    setPendingStartChoice(null);
  }

  function enterBuildMode(nextMode) {
    const switching = buildMode !== nextMode;
    setGeoError("");
    setSelectedSuggestionId("");
    setSuggestionPreview(null);
    cancelRouteModePicker();
    cancelStartPicker();

    if (nextMode === "suggest") {
      if (switching) {
        setStartChoice(null);
        setOrigin(null);
        setStart("");
        setSelectedIds([]);
        setDraft(null);
      }
      setBuildMode("suggest");
      return;
    }

    if (switching) {
      setStartChoice(null);
      setOrigin(null);
      setStart("");
      setSelectedIds([]);
      setNodeCatalog({});
      setViewFocus(null);
      setDraft(null);
      reverseKeyRef.current = "";
    }

    setBuildMode(nextMode);
    if (nextMode === "manual") {
      setMode("punt");
    }
    if (switching || !startChoice) {
      setStartPickerOpen(true);
    }
  }

  function confirmRouteMode(nextMode) {
    setMode(nextMode);
    setModePickerOpen(false);
    const pending = pendingStartChoice;
    setPendingStartChoice(null);
    if (pending === "map") chooseMapStart();
    else if (pending === "gps") chooseMyLocation();
  }

  async function goToSearchPlace({ lat, lng, label, zoom = 12 }) {
    onPreview({ lat, lng, zoom });
    if (buildMode === "suggest" || startChoice !== "map") return;
    setSelectedIds([]);
    setDraft(null);
    setViewFocus({ lat, lng });
    const placeLabel = label ? shortPlaceLabel(label) : undefined;
    await setFromCoords({ lat, lng }, "map", placeLabel);
    setLocateTick((tick) => tick + 1);
  }

  async function chooseMyLocation() {
    setStartChoice("gps");
    setGeoBusy(true);
    setGeoError("");
    setSelectedIds([]);
    setNodeCatalog({});
    setViewFocus(null);
    setDraft(null);
    try {
      const next = await getBrowserLocation();
      setHere(next);
      await setFromCoords(next, "gps");
      setLocateTick((tick) => tick + 1);
    } catch (err) {
      setGeoError(err.message);
      setStartChoice(null);
    } finally {
      setGeoBusy(false);
    }
  }

  async function pickOnMap(next) {
    if (buildMode === "suggest") return;
    if (startChoice !== "map") return;
    setGeoError("");
    if (buildMode === "manual") {
      setSelectedIds([]);
      setDraft(null);
    }
    setViewFocus({ lat: next.lat, lng: next.lng });
    await setFromCoords(next, "map");
    setLocateTick((tick) => tick + 1);
  }

  function setStartFromKnoop(node) {
    const id = nodeId(node);
    rememberNodes(node);
    setStartChoice((current) => current || "map");
    setOrigin({ lat: node.lat, lng: node.lng, source: "knoop" });
    setStart(`Knooppunt ${node.number}`);
    setGeoError("");
    reverseKeyRef.current = `${node.lat.toFixed(4)},${node.lng.toFixed(4)}`;
    if (buildMode === "manual") setSelectedIds([id]);
    else setSelectedIds([]);
  }

  function toggleNode(node) {
    if (buildMode === "auto") {
      if (startChoice === "gps" && origin) return;
      setStartFromKnoop(node);
      return;
    }
    if (buildMode !== "manual") return;
    const id = nodeId(node);

    if (selectedIds.length === 0) {
      if (startChoice === "gps" && origin) {
        rememberNodes(node);
        if (mode === "punt") {
          setOrigin({ lat: node.lat, lng: node.lng, source: "knoop" });
          setStart(`Knooppunt ${node.number}`);
        }
        setSelectedIds([id]);
        return;
      }
      setStartFromKnoop(node);
      return;
    }

    setSelectedIds((current) => {
      if (current.includes(id)) {
        const next = current.filter((item) => item !== id);
        if (origin?.source === "knoop" && current[0] === id) {
          if (next.length) {
            const replacement = nodeLookup.get(next[0]);
            if (replacement) {
              setOrigin({ lat: replacement.lat, lng: replacement.lng, source: "knoop" });
              setStart(`Knooppunt ${replacement.number}`);
            }
          } else if (startChoice === "map") {
            setOrigin(null);
            setStart("");
          }
        }
        return next;
      }
      rememberNodes(node);
      return [...current, id];
    });
  }

  function nodeVariant(node) {
    if (mode === "punt" && selectedNodes.length) {
      const first = selectedNodes[0];
      const last = selectedNodes[selectedNodes.length - 1];
      if (knoopMatches(first, node, 80)) return "start";
      if (selectedNodes.length >= 2 && knoopMatches(last, node, 80)) return "end";
    } else if (origin?.source === "knoop" && knoopMatches(origin, node, 80)) {
      return "start";
    }
    const id = nodeId(node);
    // Gekozen én tussenliggende knooppunten op de route: altijd groen.
    if (
      selectedIdSet.has(id) ||
      selectedNodes.some((picked) => knoopMatches(picked, node, 80)) ||
      node.on_route ||
      knoopOnRoute(node, routeNodes) ||
      knoopOnGeometry(node, draft?.geometry || routePreview?.geometry)
    ) {
      return "picked";
    }
    // In handmatige modus meteen rood tonen, niet grijs/paars-achtig idle.
    if (buildMode === "manual" || (buildMode === "auto" && startChoice === "map")) {
      return "route";
    }
    return "idle";
  }

  function isSelectedNode(node) {
    const id = nodeId(node);
    if (selectedIdSet.has(id)) return true;
    return selectedNodes.some((picked) => knoopMatches(picked, node, 80));
  }

  function submit(event) {
    event.preventDefault();
    if (buildMode !== "suggest" && !startChoice) {
      setGeoError("Kies eerst je startpunt.");
      setStartPickerOpen(true);
      return;
    }
    if (!origin && buildMode !== "suggest") {
      setGeoError(
        startChoice === "map"
          ? "Klik op de kaart of kies een startknooppunt."
          : "Kies eerst een startpunt via ‘Gebruik mijn locatie’.",
      );
      return;
    }
    if (buildMode === "manual" && !selectedNodes.length) {
      setGeoError("Kies minstens één knooppunt op de kaart.");
      return;
    }
    if (buildMode === "manual" && mode === "punt" && selectedNodes.length < 2) {
      setGeoError("Kies minstens twee knooppunten: het eerste is A, het laatste is B.");
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
    const manualPunt = buildMode === "manual" && mode === "punt" && selectedNodes.length >= 2;
    const planStart =
      manualPunt || (buildMode === "manual" && mode === "punt" && selectedNodes.length === 1)
        ? `${selectedNodes[0].lat},${selectedNodes[0].lng}`
        : origin
          ? `${origin.lat.toFixed(5)}, ${origin.lng.toFixed(5)}`
          : start;
    const planEnd =
      manualPunt
        ? `${selectedNodes[selectedNodes.length - 1].lat},${selectedNodes[selectedNodes.length - 1].lng}`
        : mode === "punt"
          ? end || null
          : null;
    onPlan({
      start: planStart,
      end: planEnd,
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
              geoid: node.geoid ?? null,
            }))
          : [],
      poi_picks: [],
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
            onClick={() => enterBuildMode("manual")}
          >
            <strong>Zelf knooppunten kiezen</strong>
            <span>Klik de nummers op de kaart. Je ziet meteen hoeveel kilometer de route al is.</span>
          </button>
          <button
            type="button"
            className={`choice-card ${buildMode === "auto" ? "on" : ""}`}
            onClick={() => enterBuildMode("auto")}
          >
            <strong>Plan mijn tocht</strong>
            <span>Geef afstand of tijd. De gids kiest knooppunten en plekken op basis van je profiel.</span>
          </button>
          <button
            type="button"
            className={`choice-card ${buildMode === "suggest" ? "on" : ""}`}
            onClick={() => enterBuildMode("suggest")}
          >
            <strong>Route Top 10</strong>
            <span>Tien kant-en-klare tochten (~50 km) rond Vlaamse steden en bezienswaardigheden.</span>
          </button>
        </div>

          {geoError && <div className="error">{geoError}</div>}

          {(buildMode === "manual" || buildMode === "auto") && startChoice && (
            <p className="sources start-status" style={{ margin: "0 0 12px" }}>
              Start:{" "}
              <strong>
                {startChoice === "gps"
                  ? start && !COORD_QUERY.test(start)
                    ? start
                    : "mijn locatie"
                  : start && !COORD_QUERY.test(start)
                    ? start
                    : "positie op de kaart"}
              </strong>
              {" · "}
              {buildMode === "manual" ? "Van A naar B" : mode === "lus" ? "Lus" : "Van A naar B"}
              {" · "}
              <button type="button" className="ghost-link" onClick={() => setStartPickerOpen(true)}>
                Wijzig startpunt
              </button>
            </p>
          )}

          {(buildMode === "manual" || buildMode === "auto") && !startChoice && (
            <p className="sources" style={{ margin: "0 0 12px" }}>
              <button type="button" className="ghost-link" onClick={() => setStartPickerOpen(true)}>
                Kies startpunt
              </button>
            </p>
          )}

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
                  : `${suggestPreviewKnooppunten.length} knooppunten in volgorde`}
              </p>
              <ol className="picked-list route-knoop-list">
                {suggestPreviewKnooppunten.map((node, index) => (
                  <li key={`${node.id || node.number}-${index}`}>
                    <span className="num">{index + 1}</span>
                    <span>
                      <strong>Knooppunt {node.number}</strong>
                      {index > 0 &&
                        index === suggestPreviewKnooppunten.length - 1 &&
                        knoopMatches(node, suggestPreviewKnooppunten[0], 80) && (
                          <small> · start</small>
                        )}
                    </span>
                  </li>
                ))}
              </ol>
            </div>
          )}

          {buildMode === "manual" && (
          <div className="editor draft-box">
            <strong>Route via knooppunten</strong>
            <p className="sources" style={{ margin: "6px 0 8px" }}>
              {!startChoice
                ? "Kies eerst je startpunt."
                : nodesBusy
                  ? "Knooppunten worden geladen..."
                  : selectedNodes.length
                    ? `${routeNodes.length} knooppunten in volgorde${
                        routeNodes.length > selectedNodes.length
                          ? ` (${selectedNodes.length} gekozen, rest via netwerk)`
                          : ""
                      }`
                    : nodes.length
                    ? startChoice === "gps"
                      ? mode === "punt"
                        ? "Klik knooppunt A, daarna B. Tussenliggende knooppunten komen automatisch mee."
                        : "Klik nummers op de kaart. Overgeslagen knooppunten worden automatisch via het netwerk toegevoegd."
                      : mode === "punt"
                        ? "Klik knooppunt A, daarna B (en eventueel tussendoor). Overgeslagen knooppunten komen automatisch mee."
                        : "Klik eerst een startknooppunt, daarna volgende. Overgeslagen knooppunten komen automatisch mee."
                    : "Nog geen knooppunten in beeld. Kies een startpositie."}
            </p>
            {routeNodes.length ? (
              <ol className="picked-list route-knoop-list">
                {routeNodes.map((node, index) => {
                  const picked = isSelectedNode(node);
                  return (
                    <li key={`${nodeId(node)}-${index}`} className={picked ? "picked-stop" : "via-stop"}>
                      <span className="num">{index + 1}</span>
                      <span>
                        <strong>Knooppunt {node.number}</strong>
                        {picked && mode === "punt" && selectedNodes.length && knoopMatches(selectedNodes[0], node, 80) && (
                          <small> · A</small>
                        )}
                        {picked &&
                          mode === "punt" &&
                          selectedNodes.length >= 2 &&
                          knoopMatches(selectedNodes[selectedNodes.length - 1], node, 80) && (
                            <small> · B</small>
                          )}
                        {picked ? <small> gekozen</small> : <small> · via netwerk</small>}
                        {index > 0 &&
                          mode !== "punt" &&
                          index === routeNodes.length - 1 &&
                          knoopMatches(node, routeNodes[0], 80) && (
                            <small> · start</small>
                          )}
                      </span>
                      {picked && (
                        <button type="button" className="ghost-mini" onClick={() => toggleNode(node)}>
                          ×
                        </button>
                      )}
                    </li>
                  );
                })}
              </ol>
            ) : (
              <p className="sources" style={{ margin: 0 }}>
                Nog geen knooppunten gekozen.
              </p>
            )}
            <div className="stats">
              <div className="stat">
                <span>Afstand</span>
                <b>{routeNodes.length ? formatKm(liveKm) : "0 km"}</b>
              </div>
              <div className="stat">
                <span>Rijtijd</span>
                <b>{liveMin ? formatDuration(liveMin) : selectedNodes.length ? "…" : "—"}</b>
              </div>
              <div className="stat">
                <span>Knooppunten</span>
                <b>{routeNodes.length}</b>
              </div>
            </div>
            {draftBusy && selectedNodes.length > 0 && (
              <p className="sources" style={{ margin: 0 }}>
                Fietsroute wordt herberekend…
              </p>
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
                    Hoeveel tijd? <b>{formatDuration(duration)}</b>
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
                      : `${previewKnooppunten.length} knooppunten · voorbeeld ${routePreview.distance_km} km`}
                  </p>
                  <ol className="picked-list route-knoop-list">
                    {previewKnooppunten.map((node, index) => (
                      <li key={`${node.id || node.number}-${index}`}>
                        <span className="num">{index + 1}</span>
                        <span>
                          <strong>Knooppunt {node.number}</strong>
                          {index > 0 &&
                            index === previewKnooppunten.length - 1 &&
                            knoopMatches(node, previewKnooppunten[0], 80) && (
                              <small> · start</small>
                            )}
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

          {buildMode === "auto" && notes.trim() && (wishOnRoute.length > 0 || wishOffRoute.length > 0) && (
            <div className="poi-suggest-section">
              {wishOnRoute.length > 0 && (
                <>
                  <strong>Op je route</strong>
                  <p className="sources" style={{ margin: "6px 0 8px" }}>
                    Deze plekken liggen langs je voorgestelde traject.
                  </p>
                  <div className="wish-on-route-list">
                    {wishOnRoute.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className={`wish-on-route ${focusedWishId === item.id ? "focus" : ""}`}
                        onClick={() => {
                          setFocusedWishId(item.id);
                          onPreview({ lat: item.lat, lng: item.lng, zoom: 15 });
                        }}
                      >
                        <span className="wish-on-route-dot" aria-hidden="true" />
                        <span>
                          <strong>{item.name}</strong>
                          <small>{item.kind_label || item.kind}</small>
                        </span>
                      </button>
                    ))}
                  </div>
                </>
              )}
              {wishOffRoute.length > 0 && (
                <>
                  <strong style={{ display: "block", marginTop: wishOnRoute.length ? 14 : 0 }}>
                    Suggesties voor je wens
                  </strong>
                  <p className="sources" style={{ margin: "6px 0 8px" }}>
                    Niet direct op het traject, maar wel in de buurt.
                  </p>
                  <div className="poi-suggest-grid">
                    {wishOffRoute.map((item) => (
                      <button
                        key={item.id}
                        type="button"
                        className={`poi-suggest-tile ${focusedWishId === item.id ? "focus" : ""}`}
                        onClick={() => {
                          setFocusedWishId(item.id);
                          onPreview({ lat: item.lat, lng: item.lng, zoom: 14 });
                        }}
                      >
                        <span className="poi-suggest-kind">{item.kind_label || item.kind}</span>
                        <strong>{item.name}</strong>
                      </button>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}

          {buildMode === "auto" && notes.trim() && routePreviewBusy && !wishSuggestions.length && (
            <p className="sources" style={{ margin: "0 0 12px" }}>
              Plekken voor je wens worden gezocht...
            </p>
          )}

          {buildMode === "auto" && notes.trim() && !routePreviewBusy && !wishSuggestions.length && routePreview?.geometry?.length > 1 && (
            <p className="sources" style={{ margin: "0 0 12px" }}>
              Geen passende plekken gevonden voor je wens. Probeer een andere formulering.
            </p>
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

          <p className="sources">{MAP_SOURCES}</p>
        </div>
      </section>

      <section className="hero-map">
        <MapContainer center={center} zoom={zoom} attributionControl zoomControl={false}>
          <TileLayer attribution={MAP_TILE.attribution} url={MAP_TILE.url} />
          <MapClick onPick={pickOnMap} />
          <MapResize />
          <MapReady onReady={setMap} />
          <MapPanFocus active={panFocusActive} onFocus={setViewFocus} />
          <MapZoomScale referenceZoom={zoom}>
          {buildMode === "manual" && draft?.geometry?.length > 1 && (
            <RouteLine positions={draft.geometry} />
          )}
          {(buildMode === "suggest" || buildMode === "auto") && routePreview?.geometry?.length > 1 && (
            <RouteLine positions={routePreview.geometry} />
          )}
          {(buildMode === "auto" || buildMode === "suggest") && routePreview?.knooppunten?.length > 0 && (
            <RoutePreviewKnoopMarkers nodes={routePreview.knooppunten} />
          )}
          {wishOnRoute.length > 0 && (
            <WishRouteMarkers items={wishOnRoute} focusedId={focusedWishId} onFocus={setFocusedWishId} />
          )}
          {(buildMode === "manual" || (buildMode === "auto" && startChoice === "map")) && (
            <PlannerKnoopMarkers
              nodes={mapNodes}
              buildMode={buildMode}
              startChoice={startChoice}
              selectedIds={selectedIds}
              origin={origin}
              nodeVariant={nodeVariant}
              onToggle={toggleNode}
            />
          )}
          {origin && origin.source === "map" && buildMode !== "suggest" && (
            <Marker position={[origin.lat, origin.lng]} icon={startIcon} zIndexOffset={1100}>
              <Popup>Zoekgebied / startpositie</Popup>
            </Marker>
          )}
          <HereMarker
            position={here && startChoice !== "map" ? here : null}
            accuracy={here && startChoice !== "map" ? here.accuracy : 0}
          />
          <MapFlyTo
            position={origin}
            trigger={locateTick}
            zoom={buildMode === "suggest" ? 12 : 14}
          />
          <Recenter
            center={center}
            zoom={zoom}
            locked={
              Boolean(origin) ||
              (buildMode === "manual" && selectedNodes.length > 0) ||
              (buildMode === "suggest" && !!selectedSuggestion)
            }
          />
          {buildMode === "manual" && lastSelectedNode && (
            <FocusLastSelected node={lastSelectedNode} trigger={selectedIds.join("|")} />
          )}
          {buildMode === "manual" &&
            nodes.length > 0 &&
            selectedNodes.length === 0 &&
            origin &&
            origin.source === "map" && (
            <FitNodes origin={origin} />
          )}
          {(buildMode === "suggest" || buildMode === "auto") && (
            <FitPreview
              geometry={routePreview?.geometry}
              active={buildMode === "suggest" ? !!selectedSuggestion : !!origin}
            />
          )}
          </MapZoomScale>
        </MapContainer>
        <div className="map-overlay">
          <MapChrome
            map={map}
            onLocate={chooseMyLocation}
            onGoTo={goToSearchPlace}
            locateDisabled={geoBusy}
            locateBusy={geoBusy}
          />
          <div className="map-hint">
          {buildMode === "manual"
            ? !startChoice
              ? "Kies je startpunt in het venster"
              : mode === "punt"
                ? selectedNodes.length >= 2
                  ? `${routeNodes.length} knooppunten · A→B · ${formatKm(liveKm)}`
                  : selectedNodes.length === 1
                    ? "Kies eindknooppunt B op de kaart"
                    : "Kies startknooppunt A op de kaart"
                : startChoice === "gps"
                  ? selectedNodes.length
                    ? `${routeNodes.length} knooppunten · ${formatKm(liveKm)}`
                    : "Kies knooppunten vanaf je locatie"
                  : origin?.source === "knoop"
                    ? selectedNodes.length > 1
                      ? `${routeNodes.length} knooppunten · ${formatKm(liveKm)}`
                      : "Kies volgende knooppunten op de kaart"
                    : "Klik een knooppunt als startpunt van je route"
            : buildMode === "suggest"
              ? selectedSuggestion
                ? routePreviewBusy
                  ? `${selectedSuggestion.title} · route laden...`
                  : `${selectedSuggestion.title} · ${distance} km`
                : "Kies een route uit de Top 10"
              : !startChoice
                ? "Kies je startpunt in het venster"
                : startChoice === "gps"
                  ? origin
                    ? "Start klaar. Stel afstand/tijd in en plan je tocht."
                    : "Je locatie wordt opgehaald…"
                  : origin
                    ? "Start klaar. Stel afstand/tijd in en plan je tocht."
                    : "Klik op de kaart of een knooppunt voor je start"}
          </div>
        </div>
      </section>

      {startPickerOpen &&
        (buildMode === "manual" || buildMode === "auto") &&
        createPortal(
          <div className="mode-picker-backdrop" onClick={cancelStartPicker}>
            <div
              className="mode-picker"
              role="dialog"
              aria-modal="true"
              aria-labelledby="start-picker-title"
              onClick={(event) => event.stopPropagation()}
            >
              <h2 id="start-picker-title" className="mode-picker-title">
                Startpunt
              </h2>
              <p className="sources" style={{ margin: "8px 0 0" }}>
                Hoe wil je je route beginnen?
              </p>
              <div className="mode-picker-options">
                <button type="button" className="choice-card mode-picker-card" onClick={requestMapStart}>
                  <strong>Kies startpositie op de kaart</strong>
                  <span>Klik op de kaart of op een knooppunt als startpunt.</span>
                </button>
                <button
                  type="button"
                  className="choice-card mode-picker-card"
                  onClick={requestMyLocation}
                  disabled={geoBusy}
                >
                  <strong>{geoBusy ? "GPS wordt opgehaald…" : "Gebruik mijn locatie"}</strong>
                  <span>Start vanaf waar je nu bent.</span>
                </button>
              </div>
              <button type="button" className="ghost mode-picker-cancel" onClick={cancelStartPicker}>
                Annuleren
              </button>
            </div>
          </div>,
          document.body,
        )}

      {modePickerOpen &&
        buildMode === "auto" &&
        createPortal(
          <div className="mode-picker-backdrop" onClick={cancelRouteModePicker}>
            <div
              className="mode-picker"
              role="dialog"
              aria-modal="true"
              aria-labelledby="mode-picker-title"
              onClick={(event) => event.stopPropagation()}
            >
              <h2 id="mode-picker-title" className="mode-picker-title">
                Welk type route?
              </h2>
              <p className="sources" style={{ margin: "8px 0 0" }}>
                {pendingStartChoice === "gps"
                  ? "Je start vanaf je locatie."
                  : "Je kiest je startpositie op de kaart."}
              </p>
              <div className="mode-picker-options">
                <button
                  type="button"
                  className={`choice-card mode-picker-card ${mode === "lus" ? "on" : ""}`}
                  onClick={() => confirmRouteMode("lus")}
                >
                  <strong>Lus</strong>
                  <span>Je fietst rond en keert terug naar je startpunt.</span>
                </button>
                <button
                  type="button"
                  className={`choice-card mode-picker-card ${mode === "punt" ? "on" : ""}`}
                  onClick={() => confirmRouteMode("punt")}
                >
                  <strong>Van A naar B</strong>
                  <span>Je fietst van start naar een andere bestemming.</span>
                </button>
              </div>
              <button type="button" className="ghost mode-picker-cancel" onClick={cancelRouteModePicker}>
                Annuleren
              </button>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}

function WishRouteMarkers({ items, focusedId, onFocus }) {
  return items.map((item) => (
    <Marker
      key={item.id}
      position={[item.lat, item.lng]}
      icon={wishPoiIcon({
        interest: item.interest,
        kind: item.kind_label || item.kind,
        focused: focusedId === item.id,
      })}
      zIndexOffset={1400}
      eventHandlers={{
        click: (event) => {
          if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
          onFocus(item.id);
        },
      }}
    >
      <Popup>
        <strong>{item.name}</strong>
        <p style={{ margin: "6px 0 0" }}>
          {item.kind_label || item.kind} · op je route
        </p>
      </Popup>
    </Marker>
  ));
}

function RoutePreviewKnoopMarkers({ nodes }) {
  const { scale } = useMapZoom();
  if (!nodes?.length) return null;
  return nodes.map((node, index) => (
    <Marker
      key={`${nodeId(node)}-${index}`}
      position={[node.lat, node.lng]}
      icon={nodeIcon(node.number, "picked", scale)}
      zIndexOffset={1300}
    >
      <Popup>Knooppunt {node.number}</Popup>
    </Marker>
  ));
}

function PlannerKnoopMarkers({ nodes, buildMode, startChoice, selectedIds, origin, nodeVariant, onToggle }) {
  const { scale, showKnoopMarkers } = useMapZoom();
  if (!showKnoopMarkers) return null;
  return nodes.map((node) => {
    const variant = nodeVariant(node);
    return (
      <Marker
        key={nodeId(node)}
        position={[node.lat, node.lng]}
        icon={nodeIcon(node.number, variant, scale)}
        zIndexOffset={variant === "picked" || variant === "start" || variant === "end" ? 1200 : 1000}
        eventHandlers={{
          click: (event) => {
            if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
            onToggle(node);
          },
        }}
      />
    );
  });
}

function MapPanFocus({ active, delayMs = 1800, onFocus }) {
  const map = useMap();
  const timerRef = useRef(null);
  const onFocusRef = useRef(onFocus);
  onFocusRef.current = onFocus;

  const schedule = () => {
    if (!active) return;
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      const center = map.getCenter();
      onFocusRef.current({ lat: center.lat, lng: center.lng });
    }, delayMs);
  };

  useMapEvents({
    dragstart() {
      clearTimeout(timerRef.current);
    },
    moveend: schedule,
    zoomend: schedule,
  });

  useEffect(() => {
    if (!active) {
      clearTimeout(timerRef.current);
      return undefined;
    }
    schedule();
    return () => clearTimeout(timerRef.current);
  }, [active, delayMs, map]);

  return null;
}

function MapClick({ onPick }) {
  useMapEvents({
    click(event) {
      const target = event.originalEvent?.target;
      if (target?.closest?.(".leaflet-marker-icon, .leaflet-popup, .leaflet-control, .map-chrome")) return;
      onPick({ lat: event.latlng.lat, lng: event.latlng.lng });
    },
  });
  return null;
}

function shortPlaceLabel(label) {
  const parts = String(label || "")
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
  if (!parts.length) return "Startpunt gekozen";
  return parts.slice(0, 2).join(", ");
}

function Recenter({ center, zoom, locked }) {
  const map = useMap();
  useEffect(() => {
    if (locked) return;
    map.setView(center, zoom || map.getZoom());
  }, [center, locked, map, zoom]);
  return null;
}

function FocusLastSelected({ node, trigger }) {
  const map = useMap();
  const seen = useRef("");
  useEffect(() => {
    if (!node) return;
    const key = `${trigger}|${node.lat},${node.lng}`;
    if (key === seen.current) return;
    seen.current = key;
    map.flyTo([node.lat, node.lng], map.getZoom(), { duration: 0.45 });
  }, [map, node, trigger]);
  return null;
}

function FitNodes({ origin }) {
  const map = useMap();
  useEffect(() => {
    if (!origin) return;
    map.setView([origin.lat, origin.lng], Math.max(map.getZoom(), 14), { animate: true });
  }, [origin?.lat, origin?.lng, map]);
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
