import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MapContainer, Marker, Popup, TileLayer, useMap, useMapEvents } from "react-leaflet";
import L from "leaflet";
import { fetchKnooppunten, fetchPoiSuggestions, fetchRoutePreview, fetchRouteSuggestions, reverseGeocode, reroute } from "../api.js";
import { estimateRouteKm, formatDuration, formatKm, getBrowserLocation, knoopMatches, knoopOnRoute, mergeMapKnooppunten, nodeId, poiId, dedupeNearbyPoints } from "../geo.js";
import { useDebounced } from "../hooks.js";
import { nodeIcon, poiIcon, startIcon } from "../icons.js";
import { profileSummary, suggestedDistance, suggestedMinutes, toApiProfile, mergeInterests, interestLabels } from "../profile.js";
import { MAP_SOURCES, MAP_TILE } from "../mapTiles.js";
import HereMarker from "./HereMarker.jsx";
import MapChrome from "./MapChrome.jsx";
import MapFlyTo, { MAP_FLY_PADDING } from "./MapFlyTo.jsx";
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
  const [pendingStartChoice, setPendingStartChoice] = useState(null); // "map" | "gps"
  const [poiSuggestions, setPoiSuggestions] = useState([]);
  const [poiSuggestionsBusy, setPoiSuggestionsBusy] = useState(false);
  const [poiSuggestionsError, setPoiSuggestionsError] = useState("");
  const [poiCatalog, setPoiCatalog] = useState({});
  const [routePoiIds, setRoutePoiIds] = useState([]);
  const [focusedPoi, setFocusedPoi] = useState(null);
  const [poiPickerOpen, setPoiPickerOpen] = useState(false);
  const [poiFocusTarget, setPoiFocusTarget] = useState(null);
  const mapRef = useRef(null);
  const poiFetchGen = useRef(0);
  const poiPickerTimerRef = useRef(null);
  originRef.current = origin;
  mapRef.current = map;
  const reverseKeyRef = useRef("");
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

  const routeNodes = useMemo(
    () => (draft?.knooppunten?.length ? draft.knooppunten : selectedNodes),
    [draft, selectedNodes],
  );

  const selectedIdSet = useMemo(() => new Set(selectedIds), [selectedIds]);

  const mapNodes = useMemo(
    () => mergeMapKnooppunten(nodes, routeNodes, selectedNodes),
    [nodes, routeNodes, selectedNodes],
  );

  const estimateKm = estimateRouteKm(origin, selectedNodes, mode !== "punt");
  const liveKm = draft?.distance_km ?? estimateKm;
  const liveMin = draft?.duration_min;

  const poiSamplePoints = useMemo(() => {
    const raw = [];
    if (origin?.lat != null && origin?.lng != null) {
      raw.push({ lat: origin.lat, lng: origin.lng });
    }
    for (const node of selectedNodes) {
      raw.push({ lat: node.lat, lng: node.lng });
    }
    const geometry =
      draft?.geometry?.length > 1
        ? draft.geometry
        : suggestionPreview?.geometry?.length > 1
          ? suggestionPreview.geometry
          : null;
    if (geometry) {
      const step = Math.max(1, Math.floor(geometry.length / 4));
      for (let index = 0; index < geometry.length; index += step) {
        raw.push({ lat: geometry[index][0], lng: geometry[index][1] });
      }
    } else if (!selectedNodes.length && routeNodes.length) {
      const step = Math.max(1, Math.floor(routeNodes.length / 3));
      for (let index = 0; index < routeNodes.length; index += step) {
        raw.push({ lat: routeNodes[index].lat, lng: routeNodes[index].lng });
      }
    }
    return dedupeNearbyPoints(raw, 700).slice(0, 4);
  }, [origin, selectedNodes, routeNodes, draft?.geometry, suggestionPreview?.geometry]);

  const poiFocus = useMemo(() => poiSamplePoints[0] || (origin?.lat != null ? { lat: origin.lat, lng: origin.lng } : null), [
    poiSamplePoints,
    origin,
  ]);

  const routePois = useMemo(
    () => routePoiIds.map((id) => poiCatalog[id]).filter(Boolean),
    [routePoiIds, poiCatalog],
  );

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
      .split("|")
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
    const poiPicks = routePoiIds.map((id) => poiCatalog[id]).filter(Boolean);
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
      poi_picks: poiPicks.map((poi) => ({
        id: poi.id,
        name: poi.name,
        lat: poi.lat,
        lng: poi.lng,
        kind: poi.kind,
        kind_label: poi.kind_label || null,
        interest: poi.interest || "geschiedenis",
      })),
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
  }, [buildMode, mode, nodeLookup, origin, selectedKey, routePoiIds.join("|"), poiCatalog]);

  useEffect(() => {
    if (profile?.interests?.length) setInterests(profile.interests);
  }, [profile]);

  const activeInterests = profile?.interests?.length ? profile.interests : interests;

  const poiRouteReady =
    buildMode === "manual"
      ? Boolean(startChoice && selectedNodes.length > 0)
      : buildMode === "auto"
        ? Boolean(startChoice && origin)
        : false;

  const poiSuggestKey = useDebounced(
    poiRouteReady && poiFocus
      ? `${poiFocus.lat}|${poiFocus.lng}|${activeInterests.join(",")}|${poiSamplePoints.length}|${draft?.geometry?.length || 0}|${suggestionPreview?.geometry?.length || 0}|${selectedNodes.length}`
      : "",
    500,
  );

  useEffect(() => {
    if (!poiSuggestKey || !poiFocus) {
      setPoiSuggestions([]);
      setPoiSuggestionsBusy(false);
      setPoiSuggestionsError("");
      return undefined;
    }
    const gen = ++poiFetchGen.current;
    let cancelled = false;
    setPoiSuggestionsBusy(true);
    setPoiSuggestionsError("");
    const samples = poiSamplePoints.slice(1);
    fetchPoiSuggestions(poiFocus.lat, poiFocus.lng, activeInterests, {
      radius: routeNodes.length > 1 ? 6500 : 8000,
      samples,
    })
      .then((next) => {
        if (cancelled || gen !== poiFetchGen.current) return;
        setPoiSuggestions(next);
        setPoiCatalog((current) => {
          const merged = { ...current };
          for (const poi of next) merged[poiId(poi)] = poi;
          return merged;
        });
        if (!next.length) {
          setPoiSuggestionsError("Geen plekken gevonden langs je route. Probeer extra interesses in je profiel.");
        }
      })
      .catch((err) => {
        if (cancelled || gen !== poiFetchGen.current) return;
        setPoiSuggestions([]);
        setPoiSuggestionsError(err.message || "Suggesties konden niet geladen worden.");
      })
      .finally(() => {
        if (gen === poiFetchGen.current) setPoiSuggestionsBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [poiSuggestKey, poiFocus?.lat, poiFocus?.lng, poiSamplePoints, activeInterests.join("|"), routeNodes.length, selectedNodes.length]);

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
    setPendingStartChoice("map");
    setModePickerOpen(true);
  }

  function requestMyLocation() {
    setPendingStartChoice("gps");
    setModePickerOpen(true);
  }

  function cancelRouteModePicker() {
    setModePickerOpen(false);
    setPendingStartChoice(null);
  }

  function confirmRouteMode(nextMode) {
    setMode(nextMode);
    setModePickerOpen(false);
    const pending = pendingStartChoice;
    setPendingStartChoice(null);
    if (pending === "map") chooseMapStart();
    else if (pending === "gps") chooseMyLocation();
  }

  function rememberPois(...items) {
    setPoiCatalog((current) => {
      const next = { ...current };
      for (const poi of items) {
        if (!poi) continue;
        next[poiId(poi)] = poi;
      }
      return next;
    });
  }

  function focusMapOnPoi(lat, lng) {
    const fly = () => {
      mapRef.current?.flyTo([lat, lng], 15, { duration: 0.75, ...MAP_FLY_PADDING });
    };
    fly();
    requestAnimationFrame(fly);
    setTimeout(fly, 80);
  }

  function openPoiPicker(poi) {
    const lat = Number(poi?.lat);
    const lng = Number(poi?.lng ?? poi?.lon);
    if (!poi || !Number.isFinite(lat) || !Number.isFinite(lng)) return;
    const point = { ...poi, lat, lng };
    rememberPois(point);
    setPoiPickerOpen(false);
    setFocusedPoi(point);
    setPoiFocusTarget({ lat, lng, key: Date.now() });
    onPreview({ lat, lng, zoom: 15 });
    focusMapOnPoi(lat, lng);
    document.querySelector(".hero-map")?.scrollIntoView({ behavior: "smooth", block: "start" });
    clearTimeout(poiPickerTimerRef.current);
    poiPickerTimerRef.current = setTimeout(() => {
      setPoiPickerOpen(true);
    }, 780);
  }

  function closePoiPicker() {
    clearTimeout(poiPickerTimerRef.current);
    setPoiPickerOpen(false);
    setFocusedPoi(null);
  }

  function addFocusedPoiToRoute() {
    if (!focusedPoi) return;
    const id = poiId(focusedPoi);
    rememberPois(focusedPoi);
    setRoutePoiIds((current) => (current.includes(id) ? current : [...current, id]));
    closePoiPicker();
  }

  function removeFocusedPoiFromRoute() {
    if (!focusedPoi) return;
    setRoutePoiIds((current) => current.filter((item) => item !== poiId(focusedPoi)));
    closePoiPicker();
  }

  function renderInterestSuggestions() {
    if ((buildMode !== "manual" && buildMode !== "auto") || !startChoice) return null;
    const routeReady =
      buildMode === "manual" ? selectedNodes.length > 0 : Boolean(origin);
    if (!routeReady) return null;
    return (
      <div className="poi-suggest-section" id="suggestieoverzicht">
        <strong>Suggestieoverzicht</strong>
        <p className="sources" style={{ margin: "6px 0 8px" }}>
          {poiSuggestionsBusy && !poiSuggestions.length
            ? "Plekken langs je route worden geladen..."
            : poiSuggestions.length
              ? "Klik een suggestie — de kaart gaat naar die plek op je route."
              : "Suggesties op basis van je interesses, langs je gekozen route."}
        </p>
        {poiSuggestionsError && !poiSuggestionsBusy && !poiSuggestions.length && (
          <p className="error" style={{ margin: "0 0 8px" }}>
            {poiSuggestionsError}
          </p>
        )}
        {poiSuggestions.length > 0 && (
          <div className="poi-suggest-grid">
            {poiSuggestions.map((poi) => {
              const id = poiId(poi);
              const onRoute = routePoiIds.includes(id);
              const focused = focusedPoi && poiId(focusedPoi) === id;
              return (
                <button
                  key={id}
                  type="button"
                  className={`poi-suggest-tile ${onRoute ? "on" : ""} ${focused ? "focus" : ""}`}
                  onPointerUp={(event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    openPoiPicker(poi);
                  }}
                >
                  <span className="poi-suggest-kind">{poi.kind_label || poi.kind}</span>
                  <strong>{poi.name}</strong>
                </button>
              );
            })}
          </div>
        )}
        {poiSuggestionsBusy && poiSuggestions.length > 0 && (
          <p className="sources" style={{ margin: "8px 0 0" }}>
            Meer plekken worden geladen...
          </p>
        )}
        {routePois.length > 0 && (
          <p className="sources" style={{ margin: "8px 0 0" }}>
            {routePois.length} plek{routePois.length === 1 ? "" : "ken"} toegevoegd — route wordt aangepast
            {draftBusy ? "…" : "."}
          </p>
        )}
      </div>
    );
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
    if (selectedIdSet.has(id) || selectedNodes.some((picked) => knoopMatches(picked, node, 80))) {
      return "picked";
    }
    if (node.on_route || knoopOnRoute(node, routeNodes)) {
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
    return selectedNodes.some(
      (picked) =>
        String(picked.number) === String(node.number) &&
        Math.abs(picked.lat - node.lat) < 0.0008 &&
        Math.abs(picked.lng - node.lng) < 0.0008,
    );
  }

  function submit(event) {
    event.preventDefault();
    if (buildMode !== "suggest" && !startChoice) {
      setGeoError("Kies eerst: startpositie op de kaart, of gebruik mijn locatie.");
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
      poi_picks: routePois.map((poi) => ({
        id: poi.id,
        name: poi.name,
        lat: poi.lat,
        lng: poi.lng,
        kind: poi.kind,
        kind_label: poi.kind_label || null,
        interest: poi.interest,
      })),
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
              cancelRouteModePicker();
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
              cancelRouteModePicker();
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
              cancelRouteModePicker();
              setBuildMode("suggest");
            }}
          >
            <strong>Route Top 10</strong>
            <span>Tien kant-en-klare tochten (~50 km) rond Vlaamse steden en bezienswaardigheden.</span>
          </button>
        </div>

          {buildMode === "manual" && (
          <>
          <div className="start-choice">
            <span className="start-pick-label">Startpunt</span>
            <div className="row">
              <button
                type="button"
                className={`mode ${startChoice === "map" ? "on" : ""}`}
                onClick={requestMapStart}
              >
                Kies startpositie op de kaart
              </button>
              <button
                type="button"
                className={`mode ${startChoice === "gps" ? "on" : ""}`}
                onClick={requestMyLocation}
                disabled={geoBusy}
              >
                {geoBusy && startChoice === "gps" ? "GPS..." : "Gebruik mijn locatie"}
              </button>
            </div>
            {startChoice && (
              <p className="sources" style={{ margin: "6px 0 0" }}>
                Type route: <strong>{mode === "lus" ? "Lus" : "Van A naar B"}</strong>
              </p>
            )}
            {startChoice && (
              <p className="sources" style={{ margin: "8px 0 0" }}>
                {startChoice === "gps"
                  ? origin
                    ? mode === "punt"
                      ? selectedNodes.length
                        ? selectedNodes.length >= 2
                          ? `Van knooppunt ${selectedNodes[0].number} naar ${selectedNodes[selectedNodes.length - 1].number}.`
                          : `Start A: knooppunt ${selectedNodes[0].number}. Kies nog een eindknooppunt (B).`
                        : "Klik het eerste knooppunt (A), daarna het laatste (B)."
                      : `Vanaf je locatie${start && !COORD_QUERY.test(start) ? ` (${start})` : ""}. Kies knooppunten op de kaart.`
                    : "Je locatie wordt opgehaald…"
                  : origin?.source === "knoop"
                    ? mode === "punt"
                      ? selectedNodes.length >= 2
                        ? `Van knooppunt ${selectedNodes[0].number} naar ${selectedNodes[selectedNodes.length - 1].number}.`
                        : `Start A: knooppunt ${selectedNodes[0].number}. Kies het eindknooppunt (B).`
                      : `Startknooppunt: ${start}. Kies daarna volgende knooppunten.`
                    : origin
                      ? mode === "punt"
                        ? "Klik het eerste knooppunt (A), daarna het laatste (B)."
                        : "Kies nu een knooppunt op de kaart als startpunt van je route."
                      : mode === "punt"
                        ? "Klik het eerste knooppunt (A), daarna het laatste (B)."
                        : "Klik op de kaart om te zoomen, of kies meteen een startknooppunt."}
              </p>
            )}
          </div>
          {geoError && <div className="error">{geoError}</div>}

          </>
          )}

          {buildMode === "auto" && (
          <>
          <div className="start-choice">
            <span className="start-pick-label">Startpunt</span>
            <div className="row">
              <button
                type="button"
                className={`mode ${startChoice === "map" ? "on" : ""}`}
                onClick={requestMapStart}
              >
                Kies startpositie op de kaart
              </button>
              <button
                type="button"
                className={`mode ${startChoice === "gps" ? "on" : ""}`}
                onClick={requestMyLocation}
                disabled={geoBusy}
              >
                {geoBusy && startChoice === "gps" ? "GPS..." : "Gebruik mijn locatie"}
              </button>
            </div>
            {startChoice && (
              <p className="sources" style={{ margin: "6px 0 0" }}>
                Type route: <strong>{mode === "lus" ? "Lus" : "Van A naar B"}</strong>
              </p>
            )}
            {startChoice && (
              <p className="sources" style={{ margin: "8px 0 0" }}>
                {startChoice === "gps"
                  ? origin
                    ? `Start: ${start && !COORD_QUERY.test(start) ? start : "jouw locatie"}`
                    : "Je locatie wordt opgehaald…"
                  : origin
                    ? `Start: ${start && !COORD_QUERY.test(start) ? start : "positie op de kaart"}`
                    : "Klik op de kaart (of een knooppunt) om te starten."}
              </p>
            )}
          </div>
          {geoError && <div className="error">{geoError}</div>}

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
            <strong>Route via knooppunten</strong>
            <p className="sources" style={{ margin: "6px 0 8px" }}>
              {!startChoice
                ? "Kies eerst je startmethode hierboven."
                : nodesBusy
                  ? "Knooppunten worden geladen..."
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
                        {!picked && draft?.knooppunten?.length > selectedNodes.length && (
                          <small> · via netwerk</small>
                        )}
                        {picked && index < routeNodes.length - 1 && (
                          <small> gekozen</small>
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

          {buildMode === "manual" && renderInterestSuggestions()}

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

          {buildMode === "auto" && renderInterestSuggestions()}

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
          {(buildMode === "manual" || buildMode === "auto") &&
            (poiSuggestions.length > 0 || routePois.length > 0 || focusedPoi) && (
            <PlannerPoiMarkers
              pois={[
                ...poiSuggestions,
                ...routePois.filter((poi) => !poiSuggestions.some((item) => poiId(item) === poiId(poi))),
                ...(focusedPoi &&
                !poiSuggestions.some((item) => poiId(item) === poiId(focusedPoi)) &&
                !routePois.some((item) => poiId(item) === poiId(focusedPoi))
                  ? [focusedPoi]
                  : []),
              ]}
              routePoiIds={routePoiIds}
              focusedPoi={focusedPoi}
              onSelect={openPoiPicker}
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
          <PoiMapFocus target={poiFocusTarget} />
          <Recenter
            center={center}
            zoom={zoom}
            locked={
              poiPickerOpen ||
              Boolean(focusedPoi) ||
              Boolean(origin) ||
              (buildMode === "manual" && selectedNodes.length > 0) ||
              (buildMode === "suggest" && !!selectedSuggestion)
            }
          />
          {buildMode === "manual" && lastSelectedNode && !poiPickerOpen && !focusedPoi && (
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
              active={
                !poiPickerOpen &&
                !focusedPoi &&
                (buildMode === "suggest" ? !!selectedSuggestion : !!origin)
              }
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
              ? "Kies links: startpositie op de kaart, of gebruik mijn locatie"
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
                ? "Kies links: startpositie op de kaart, of gebruik mijn locatie"
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

      {poiPickerOpen &&
        focusedPoi &&
        createPortal(
          <div className="mode-picker-backdrop" onClick={closePoiPicker}>
            <div
              className="mode-picker poi-picker"
              role="dialog"
              aria-modal="true"
              aria-labelledby="poi-picker-title"
              onClick={(event) => event.stopPropagation()}
            >
              <h2 id="poi-picker-title" className="mode-picker-title">
                {focusedPoi.name}
              </h2>
              <p className="sources" style={{ margin: "8px 0 0" }}>
                {focusedPoi.kind_label || focusedPoi.kind}
              </p>
              <p className="sources" style={{ margin: "10px 0 0" }}>
                Wil je deze plek toevoegen aan je route? Je fietsroute wordt hierdoor aangepast.
              </p>
              <div className="poi-picker-actions">
                {routePoiIds.includes(poiId(focusedPoi)) ? (
                  <>
                    <button type="button" className="ghost" onClick={removeFocusedPoiFromRoute}>
                      Verwijderen van route
                    </button>
                    <button type="button" className="ghost mode-picker-cancel" onClick={closePoiPicker}>
                      Sluiten
                    </button>
                  </>
                ) : (
                  <>
                    <button type="button" className="submit" onClick={addFocusedPoiToRoute}>
                      Ja, toevoegen aan route
                    </button>
                    <button type="button" className="ghost mode-picker-cancel" onClick={closePoiPicker}>
                      Nee, overslaan
                    </button>
                  </>
                )}
              </div>
            </div>
          </div>,
          document.body,
        )}

      {modePickerOpen &&
        (buildMode === "manual" || buildMode === "auto") &&
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
                  <span>
                    {buildMode === "manual"
                      ? "Eerste knooppunt is A, laatste knooppunt is B."
                      : "Je fietst van start naar een andere bestemming."}
                  </span>
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

function PoiMapFocus({ target }) {
  const map = useMap();
  const lastKey = useRef(0);

  useEffect(() => {
    if (!target?.key || target.key === lastKey.current) return;
    lastKey.current = target.key;
    const lat = Number(target.lat);
    const lng = Number(target.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return;
    map.flyTo([lat, lng], 15, { duration: 0.75, ...MAP_FLY_PADDING });
  }, [map, target]);

  return null;
}

function PlannerPoiMarker({ poi, selected, focused, onSelect }) {
  const id = poiId(poi);

  return (
    <Marker
      key={id}
      position={[Number(poi.lat), Number(poi.lng)]}
      icon={poiIcon({ selected, focused })}
      zIndexOffset={focused ? 2000 : selected ? 1500 : 900}
      eventHandlers={{
        click: (event) => {
          if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
          onSelect(poi);
        },
      }}
    >
      <Popup>
        <strong>{poi.name}</strong>
        <p>{poi.kind_label || poi.kind}</p>
      </Popup>
    </Marker>
  );
}

function PlannerPoiMarkers({ pois, routePoiIds, focusedPoi, onSelect }) {
  if (!pois?.length) return null;
  const unique = [];
  const seen = new Set();
  for (const poi of pois) {
    const id = poiId(poi);
    if (seen.has(id)) continue;
    seen.add(id);
    unique.push(poi);
  }
  return unique.map((poi) => {
    const id = poiId(poi);
    const selected = routePoiIds.includes(id);
    const focused = focusedPoi && poiId(focusedPoi) === id;
    return (
      <PlannerPoiMarker
        key={id}
        poi={poi}
        selected={selected}
        focused={focused}
        onSelect={onSelect}
      />
    );
  });
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
        zIndexOffset={variant === "picked" || variant === "start" ? 1200 : 1000}
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
