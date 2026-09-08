import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MapContainer, Marker, Popup, TileLayer, useMap, useMapEvents } from "react-leaflet";
import L from "leaflet";
import { fetchBikeLeg, fetchKnooppunten, fetchRoutePreview, fetchRouteSuggestions, fetchWishSuggestions, isAbortError, reverseGeocode, reroute } from "../api.js";
import {
  estimateRouteKm,
  formatDuration,
  formatKm,
  haversine,
  getBrowserLocation,
  getStartupLocation,
  readCachedLocation,
  rememberLocation,
  displayLoopNodes,
  ID_JOIN,
  knoopMatches,
  knoopOnRoute,
  knoopOnGeometry,
  matchingUserPick,
  mergeMapKnooppunten,
  userPickedRouteIndexes,
  geometryProgressIndex,
  nodeId,
  poiId,
} from "../geo.js";
import { useDebounced } from "../hooks.js";
import { nodeIcon, startIcon, wishPoiSvg, wishPoiIcon } from "../icons.js";
import { profileSummary, suggestedDistance, suggestedMinutes, toApiProfile, mergeInterests, interestLabels } from "../profile.js";
import { MAP_SOURCES, MAP_TILE } from "../mapTiles.js";
import { getUsedRouteIds } from "../routeHistory.js";
import FocusPulse from "./FocusPulse.jsx";
import HereMarker from "./HereMarker.jsx";
import MapChrome from "./MapChrome.jsx";
import MapFlyTo from "./MapFlyTo.jsx";
import MapReady from "./MapReady.jsx";
import MapResize from "./MapResize.jsx";
import MapZoomScale, { useMapZoom } from "./MapZoomScale.jsx";
import RouteLine from "./RouteLine.jsx";
import "leaflet/dist/leaflet.css";

const COORD_QUERY = /^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$/;
/** Na kaartinteractie: eerst 5s stilte, daarna 15s tellen tot route-overzicht. */
const MAP_IDLE_BEFORE_COUNT_MS = 5000;
const MAP_ROUTE_OVERVIEW_MS = 15000;
/** Knooppunten rond nieuw kaartcentrum: kort na pannen/zoomen. */
const MAP_KNOOP_LOAD_DEBOUNCE_MS = 450;
let mapLastInteractAt = 0;

function noteMapUserInteraction() {
  mapLastInteractAt = Date.now();
}

/** Ms tot globaal route-overzicht; 0 = meteen toegestaan. */
function msUntilRouteOverview() {
  if (!mapLastInteractAt) return 0;
  const idleFor = Date.now() - mapLastInteractAt;
  if (idleFor < MAP_IDLE_BEFORE_COUNT_MS) {
    return MAP_IDLE_BEFORE_COUNT_MS - idleFor + MAP_ROUTE_OVERVIEW_MS;
  }
  return Math.max(0, MAP_ROUTE_OVERVIEW_MS - (idleFor - MAP_IDLE_BEFORE_COUNT_MS));
}

/** Gelijkmatig langs de route bemonsteren (op afstand), maxPoints vertices. */
function sampleGeometryAlongRoute(geometry, maxPoints = 48) {
  if (!geometry?.length) return [];
  if (geometry.length <= maxPoints) return geometry.map((point) => [...point]);
  const dists = [0];
  for (let index = 1; index < geometry.length; index += 1) {
    const prev = geometry[index - 1];
    const cur = geometry[index];
    dists.push(
      dists[index - 1] +
        haversine({ lat: prev[0], lng: prev[1] }, { lat: cur[0], lng: cur[1] }),
    );
  }
  const total = dists[dists.length - 1] || 1;
  const out = [];
  let cursor = 0;
  for (let i = 0; i < maxPoints; i += 1) {
    const target = (total * i) / (maxPoints - 1);
    while (cursor < dists.length - 1 && dists[cursor] < target) cursor += 1;
    const point = geometry[Math.min(cursor, geometry.length - 1)];
    out.push([point[0], point[1]]);
  }
  return out;
}

/** Plak een nieuw straatsegment achter de bestaande magenta lijn. */
function mergeStreetGeometries(base, extension) {
  if (!extension?.length) return base || [];
  if (!base?.length) return extension.map((point) => [...point]);
  const merged = base.map((point) => [...point]);
  const last = merged[merged.length - 1];
  const first = extension[0];
  const sameJoin =
    last &&
    first &&
    haversine({ lat: last[0], lng: last[1] }, { lat: first[0], lng: first[1] }) < 35;
  for (let index = sameJoin ? 1 : 0; index < extension.length; index += 1) {
    merged.push([extension[index][0], extension[index][1]]);
  }
  return merged;
}

export default function Planner({ busy, error, center, zoom = 14, profile, onEditProfile, onPreview, onPlan, onClearError }) {
  const [map, setMap] = useState(null);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [mode, setMode] = useState("punt");
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
  const [mapBooted, setMapBooted] = useState(() => Boolean(readCachedLocation()));
  const [mapSeed, setMapSeed] = useState(() => readCachedLocation());
  const [nodes, setNodes] = useState([]);
  const [nodeCatalog, setNodeCatalog] = useState({});
  const [viewFocus, setViewFocus] = useState(null);
  const [nodesBusy, setNodesBusy] = useState(false);
  const [selectedIds, setSelectedIds] = useState([]);
  const [draft, setDraft] = useState(null);
  const [draftBusy, setDraftBusy] = useState(false);
  const draftRef = useRef(null);
  const manualRouteIdsRef = useRef([]);
  const legCacheRef = useRef(new Map());
  const routeBuildGenRef = useRef(0);
  draftRef.current = draft;
  const [suggestions, setSuggestions] = useState([]);
  const [suggestionsBusy, setSuggestionsBusy] = useState(false);
  const [selectedSuggestionId, setSelectedSuggestionId] = useState("");
  const [suggestionPreview, setSuggestionPreview] = useState(null);
  const [suggestionPreviewBusy, setSuggestionPreviewBusy] = useState(false);
  const [manualWishSuggestions, setManualWishSuggestions] = useState([]);
  const [manualWishSummary, setManualWishSummary] = useState("");
  const [manualWishBusy, setManualWishBusy] = useState(false);
  const [manualWishError, setManualWishError] = useState("");
  const [autoWishSuggestions, setAutoWishSuggestions] = useState([]);
  const [autoWishSummary, setAutoWishSummary] = useState("");
  const [autoWishBusy, setAutoWishBusy] = useState(false);
  const [loadedPreviewKey, setLoadedPreviewKey] = useState("");
  const [modePickerOpen, setModePickerOpen] = useState(false);
  const [startPickerOpen, setStartPickerOpen] = useState(false);
  const [pendingStartChoice, setPendingStartChoice] = useState(null); // "map" | "gps"
  const [pickedWishPois, setPickedWishPois] = useState([]);
  const [focusedWishId, setFocusedWishId] = useState("");
  const [wishPickerOpen, setWishPickerOpen] = useState(false);
  const [wishPickerPoi, setWishPickerPoi] = useState(null);
  const [focusPulse, setFocusPulse] = useState(null);
  const pickedWishKey = useMemo(
    () => pickedWishPois.map((poi) => poiId(poi)).join("|"),
    [pickedWishPois],
  );
  originRef.current = origin;
  const reverseKeyRef = useRef("");
  // Zelf kiezen: meteen per knooppunt; andere modes: korte debounce.
  const selectedKeyRaw = selectedIds.join(ID_JOIN);
  const selectedKeyDebounced = useDebounced(selectedKeyRaw, 150);
  const selectedKey = buildMode === "manual" ? selectedKeyRaw : selectedKeyDebounced;
  // Wens-tekst apart debouncen: anders herstart elke letter de 10–15s preview en blijven suggesties weg.
  const debouncedNotes = useDebounced(notes, 900);
  const previewKey = useDebounced(
    origin &&
      ((buildMode === "suggest" && selectedSuggestionId) || buildMode === "auto")
      ? buildMode === "suggest"
        ? `${selectedSuggestionId}|${distance}|${origin.lat}|${origin.lng}|${mode}|${debouncedNotes}|${pickedWishKey}`
        : `${distance}|${duration}|${budgetMode}|${origin.lat}|${origin.lng}|${mode}|${debouncedNotes}|${pickedWishKey}`
      : "",
    500,
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
    // Zelf kiezen toont nooit een gesloten lus A→…→A.
    return displayLoopNodes(base, buildMode === "manual" ? false : mode !== "punt");
  }, [buildMode, draft, selectedNodes, mode]);

  const selectedIdSet = useMemo(() => new Set(selectedIds), [selectedIds]);

  const userPickedIndexes = useMemo(
    () => userPickedRouteIndexes(routeNodes, selectedNodes),
    [routeNodes, selectedNodes],
  );

  const mapNodes = useMemo(
    () =>
      mergeMapKnooppunten(
        nodes,
        // Zelf kiezen: geen via-netwerk-knopen als “op route” op de kaart markeren.
        buildMode === "manual" ? selectedNodes : routeNodes,
        selectedNodes,
        buildMode === "manual" ? null : draft?.geometry || suggestionPreview?.geometry,
      ),
    [buildMode, nodes, routeNodes, selectedNodes, draft?.geometry, suggestionPreview?.geometry],
  );

  const estimateKm = estimateRouteKm(origin, selectedNodes, mode !== "punt");
  const liveKm = draft?.distance_km ?? estimateKm;
  const liveMin = draft?.duration_min;

  // Magenta groeit meteen (eerst stub, daarna straatsegment); stippellijn = nog bezig.
  const manualRouteLine = useMemo(() => {
    if (buildMode !== "manual" || !(draft?.geometry?.length > 1)) return null;
    return { positions: draft.geometry, provisional: draftBusy };
  }, [buildMode, draft?.geometry, draftBusy]);

  const nodeLookupRef = useRef(nodeLookup);
  nodeLookupRef.current = nodeLookup;

  // Kaart pas mounten op bekende locatie (cache of GPS) — geen Gent-flash.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const applySeed = (next, { flyIfMoved = false } = {}) => {
        setHere(next);
        rememberLocation(next);
        // Geen GPS-recenter als er al een start/route is — anders springt de kaart
        // na de magenta lijn terug naar de gebruiker i.p.v. het routestartpunt.
        if (!originRef.current) {
          onPreview({ lat: next.lat, lng: next.lng, zoom: 14 });
        }
        setMapSeed((current) => {
          if (!current) return { lat: next.lat, lng: next.lng, accuracy: next.accuracy };
          if (
            flyIfMoved &&
            !originRef.current &&
            (Math.abs(current.lat - next.lat) > 0.002 || Math.abs(current.lng - next.lng) > 0.002)
          ) {
            queueMicrotask(() => setLocateTick((tick) => tick + 1));
          }
          return current;
        });
      };

      try {
        const next = await getStartupLocation();
        if (cancelled) return;
        applySeed(next, { flyIfMoved: true });
      } catch {
        if (cancelled) return;
        const cached = readCachedLocation();
        if (cached) {
          applySeed(cached);
        } else if (!originRef.current) {
          applySeed({ lat: 51.05, lng: 3.72, accuracy: 5000 });
        }
      } finally {
        if (!cancelled) setMapBooted(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [onPreview]);

  const lastSelectedNode = useMemo(
    () => (selectedNodes.length ? selectedNodes[selectedNodes.length - 1] : null),
    [selectedNodes],
  );

  const panFocusActive = buildMode === "manual" || buildMode === "auto";

  const nodeFocus = useMemo(() => {
    // Zoek/pan-focus: knooppunten laden rond wat de gebruiker bekijkt.
    if ((buildMode === "manual" || buildMode === "auto") && viewFocus) {
      return viewFocus;
    }
    if ((buildMode === "manual" || buildMode === "auto") && lastSelectedNode) {
      return { lat: lastSelectedNode.lat, lng: lastSelectedNode.lng };
    }
    if (origin) return { lat: origin.lat, lng: origin.lng };
    if (startChoice === "map" && center?.[0] != null && center?.[1] != null) {
      return { lat: center[0], lng: center[1] };
    }
    if (here) return { lat: here.lat, lng: here.lng };
    return null;
  }, [buildMode, lastSelectedNode, viewFocus, origin, startChoice, center, here]);

  // ~1 km grid: kleine pan/recenter mag de fetch niet steeds annuleren.
  const nodeFocusBucket = useMemo(() => {
    if ((buildMode !== "manual" && buildMode !== "auto") || !nodeFocus) return "";
    return `${(Math.round(nodeFocus.lat * 100) / 100).toFixed(2)},${(Math.round(nodeFocus.lng * 100) / 100).toFixed(2)}`;
  }, [buildMode, nodeFocus?.lat, nodeFocus?.lng]);
  const nodeFocusBucketDebounced = useDebounced(nodeFocusBucket, MAP_KNOOP_LOAD_DEBOUNCE_MS);
  const nodesLoadGenRef = useRef(0);

  useEffect(() => {
    if ((buildMode !== "manual" && buildMode !== "auto") || !nodeFocusBucketDebounced) {
      setNodesBusy(false);
      return undefined;
    }
    const [latS, lngS] = nodeFocusBucketDebounced.split(",");
    const lat = Number(latS);
    const lng = Number(lngS);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
      setNodesBusy(false);
      return undefined;
    }
    const loadId = ++nodesLoadGenRef.current;
    let cancelled = false;
    setNodesBusy(true);
    fetchKnooppunten(lat, lng)
      .then((next) => {
        if (cancelled || loadId !== nodesLoadGenRef.current) return;
        setNodes(Array.isArray(next) ? next : []);
        if (next?.length) rememberNodes(...next);
        setGeoError("");
      })
      .catch((err) => {
        if (!cancelled && loadId === nodesLoadGenRef.current && !isAbortError(err)) {
          setGeoError(err.message);
        }
      })
      .finally(() => {
        if (loadId === nodesLoadGenRef.current) setNodesBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [buildMode, nodeFocusBucketDebounced]);

  useEffect(() => {
    if (buildMode !== "manual" || !selectedKey) {
      setDraft(null);
      setDraftBusy(false);
      manualRouteIdsRef.current = [];
      return undefined;
    }
    const lookup = nodeLookupRef.current;
    const picked = selectedKey
      .split(ID_JOIN)
      .filter(Boolean)
      .map((id) => lookup.get(id))
      .filter(Boolean);
    if (picked.length < 2) {
      setDraft(null);
      setDraftBusy(false);
      manualRouteIdsRef.current = picked.map((node) => nodeId(node));
      return undefined;
    }

    const nextIds = picked.map((node) => nodeId(node));
    const buildId = ++routeBuildGenRef.current;
    let cancelled = false;

    const toDraftNodes = (nodes) =>
      nodes.map((node) => ({
        id: node.id || "",
        number: node.number,
        lat: node.lat,
        lng: node.lng,
        network: node.network || null,
        geoid: node.geoid ?? null,
        on_route: true,
      }));

    const mergeViaChain = (legs) => {
      const chain = [];
      for (const leg of legs) {
        const via = Array.isArray(leg.via_knooppunten) ? leg.via_knooppunten : [];
        if (!via.length) continue;
        for (const node of via) {
          if (chain.length && nodeId(chain[chain.length - 1]) === nodeId(node)) continue;
          // Ook match op number+coords als ids ontbreken
          if (
            chain.length &&
            String(chain[chain.length - 1].number) === String(node.number) &&
            Math.abs(chain[chain.length - 1].lat - node.lat) < 0.0003 &&
            Math.abs(chain[chain.length - 1].lng - node.lng) < 0.0003
          ) {
            continue;
          }
          chain.push(node);
        }
      }
      return chain.length >= 2 ? toDraftNodes(chain) : toDraftNodes(picked);
    };

    const applyLegs = (legs) => {
      const geometry = legs.reduce(
        (acc, leg) => mergeStreetGeometries(acc, leg.geometry || []),
        [],
      );
      if (geometry.length < 2) {
        throw new Error("Geen fietsroute gevonden langs de knooppunten.");
      }
      const distance_km = Number(
        legs.reduce((sum, leg) => sum + Number(leg.distance_km || 0), 0).toFixed(1),
      );
      const duration_min = Math.max(
        1,
        Math.round(legs.reduce((sum, leg) => sum + Number(leg.duration_min || 0), 0)),
      );
      const knooppunten = mergeViaChain(legs);
      return {
        geometry,
        distance_km,
        duration_min,
        knooppunten,
        knoop_chain: knooppunten.map((node) => node.number).join(" → "),
        steps: [],
        reason: "",
        weather: null,
      };
    };

    const straightLeg = (from, to) => ({
      geometry: [
        [from.lat, from.lng],
        [to.lat, to.lng],
      ],
      distance_km: Number((haversine(from, to) / 1000).toFixed(2)),
      duration_min: Math.max(1, Math.round(haversine(from, to) / 250)),
      steps: [],
    });

    const publish = (legs, { provisional = false } = {}) => {
      if (cancelled || buildId !== routeBuildGenRef.current) return;
      try {
        const next = applyLegs(legs);
        setDraft(next);
        setDraftBusy(provisional);
        setGeoError("");
        manualRouteIdsRef.current = nextIds;
      } catch (err) {
        if (!provisional) {
          setGeoError(err?.message || "Route kon niet worden berekend.");
        }
      }
    };

    setDraftBusy(true);
    setGeoError("");
    const legs = [];
    const missing = [];
    // 1) Synchronous stub: magenta verschijnt meteen (cache of rechte lijn).
    for (let index = 0; index < picked.length - 1; index += 1) {
      const from = picked[index];
      const to = picked[index + 1];
      const key = `net2|${nodeId(from)}|${nodeId(to)}`;
      if (legCacheRef.current.has(key)) {
        legs.push(legCacheRef.current.get(key));
      } else {
        legs.push(straightLeg(from, to));
        missing.push({ index, from, to, key });
      }
    }
    publish(legs, { provisional: missing.length > 0 });

    if (!missing.length) {
      rememberNodes(...picked);
      setDraftBusy(false);
      return () => {
        cancelled = true;
      };
    }

    // 2) Ontbrekende segmenten parallel ophalen via officieel knooppuntennetwerk.
    (async () => {
      const results = await Promise.all(
        missing.map(async (item) => {
          try {
            const leg = await fetchBikeLeg(item.from, item.to);
            return { ...item, leg, ok: true };
          } catch (err) {
            return { ...item, err, ok: false };
          }
        }),
      );
      if (cancelled || buildId !== routeBuildGenRef.current) return;
      let anyOk = false;
      let stillMissing = 0;
      for (const item of results) {
        if (!item.ok) {
          stillMissing += 1;
          continue;
        }
        legCacheRef.current.set(item.key, item.leg);
        legs[item.index] = item.leg;
        anyOk = true;
      }
      if (anyOk) {
        publish(legs, { provisional: stillMissing > 0 });
        rememberNodes(...picked);
        setDraftBusy(stillMissing > 0);
      } else if (!draftRef.current?.geometry?.length) {
        setGeoError(results[0]?.err?.message || "Route kon niet worden berekend. Probeer opnieuw.");
        setDraftBusy(false);
      } else {
        setDraftBusy(false);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [buildMode, selectedKey]);

  useEffect(() => {
    if (profile?.interests?.length) setInterests(profile.interests);
  }, [profile]);

  const activeInterests = profile?.interests?.length ? profile.interests : interests;

  // Manuele route: wens/profiel pas ná stabiele magenta — anders wordt elke
  // geometry-update gecanceld en blijven suggesties de hele avond weg.
  const manualWishKey = useDebounced(
    buildMode === "manual" &&
      !draftBusy &&
      draft?.geometry?.length > 1 &&
      (debouncedNotes.trim() || activeInterests.length > 0)
      ? `${selectedKey}|${debouncedNotes.trim()}|${activeInterests.join(",")}`
      : "",
    400,
  );

  useEffect(() => {
    if (buildMode !== "manual") {
      setManualWishSuggestions([]);
      setManualWishSummary("");
      setManualWishBusy(false);
      setManualWishError("");
      return undefined;
    }
    if (!manualWishKey) {
      // Tijdens magenta-herberekening bestaande suggesties behouden.
      return undefined;
    }
    const draftNow = draftRef.current;
    if (!draftNow?.geometry?.length) return undefined;
    let cancelled = false;
    setManualWishBusy(true);
    setManualWishError("");
    const geometry = sampleGeometryAlongRoute(draftNow.geometry, 48);
    const nodes = (draftNow.knooppunten || []).map((node) => ({
      id: node.id || "",
      number: node.number,
      lat: node.lat,
      lng: node.lng,
      network: node.network || null,
      geoid: node.geoid ?? null,
    }));
    const interests =
      activeInterests.length > 0 ? activeInterests : ["geschiedenis"];
    fetchWishSuggestions({
      notes: debouncedNotes.trim(),
      interests,
      geometry,
      nodes,
    })
      .then((data) => {
        if (cancelled) return;
        const items = Array.isArray(data?.suggestions) ? data.suggestions : [];
        setManualWishSuggestions(items);
        setManualWishSummary(data?.wish_summary || "");
        setManualWishError(
          items.length
            ? ""
            : "Geen passende plekken gevonden. Probeer “café” of “museum” in Extra wens.",
        );
      })
      .catch((err) => {
        if (!cancelled) {
          setManualWishSuggestions([]);
          setManualWishSummary("");
          setManualWishError(err?.message || "Wenssuggesties konden niet geladen worden.");
        }
      })
      .finally(() => {
        if (!cancelled) setManualWishBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [buildMode, manualWishKey]);

  // Auto/suggest: magenta eerst, daarna apart wens/profiel-suggesties.
  const autoWishKey = useDebounced(
    (buildMode === "auto" || buildMode === "suggest") &&
      !suggestionPreviewBusy &&
      loadedPreviewKey &&
      suggestionPreview?.geometry?.length > 1
      ? `${loadedPreviewKey}|${debouncedNotes}|${activeInterests.join(",")}`
      : "",
    400,
  );
  const suggestionPreviewRef = useRef(suggestionPreview);
  suggestionPreviewRef.current = suggestionPreview;

  useEffect(() => {
    if (buildMode !== "auto" && buildMode !== "suggest") {
      setAutoWishSuggestions([]);
      setAutoWishSummary("");
      setAutoWishBusy(false);
      return undefined;
    }
    if (!autoWishKey) return undefined;
    const previewNow = suggestionPreviewRef.current;
    if (!previewNow?.geometry?.length) return undefined;
    let cancelled = false;
    setAutoWishBusy(true);
    const geometry = sampleGeometryAlongRoute(previewNow.geometry, 48);
    const interests =
      activeInterests.length > 0 ? activeInterests : ["geschiedenis"];
    fetchWishSuggestions({
      notes: debouncedNotes.trim(),
      interests,
      geometry,
      nodes: (previewNow.knooppunten || []).map((node) => ({
        id: node.id || "",
        number: node.number,
        lat: node.lat,
        lng: node.lng,
        network: node.network || null,
        geoid: node.geoid ?? null,
      })),
    })
      .then((data) => {
        if (cancelled) return;
        setAutoWishSuggestions(Array.isArray(data?.suggestions) ? data.suggestions : []);
        setAutoWishSummary(data?.wish_summary || "");
      })
      .catch(() => {
        if (!cancelled) {
          setAutoWishSuggestions([]);
          setAutoWishSummary("");
        }
      })
      .finally(() => {
        if (!cancelled) setAutoWishBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [buildMode, autoWishKey]);

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
        if (!cancelled && !isAbortError(err)) setGeoError(err.message);
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
      setLoadedPreviewKey("");
      return undefined;
    }
    const requestKey = previewKey;
    let cancelled = false;
    setSuggestionPreviewBusy(true);
    const poiPicks = pickedWishPois;
    fetchRoutePreview({
      lat: origin.lat,
      lng: origin.lng,
      distance_km: previewDistanceKm,
      mode,
      notes: debouncedNotes,
      interests: activeInterests,
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
          setSuggestionPreview(next);
          setLoadedPreviewKey(requestKey);
          setGeoError("");
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setSuggestionPreview(null);
          setLoadedPreviewKey("");
          if (buildMode === "auto" && !isAbortError(err)) {
            setGeoError(err?.message || "Routevoorbeeld kon niet geladen worden.");
          }
        }
      })
      .finally(() => {
        if (!cancelled) setSuggestionPreviewBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [buildMode, previewKey, origin?.lat, origin?.lng, pickedWishKey]);

  function openWishPicker(poi) {
    if (!poi) return;
    const id = poiId(poi);
    setFocusedWishId(id);
    setWishPickerPoi(poi);
    setWishPickerOpen(true);
    setFocusPulse({ lat: poi.lat, lng: poi.lng, key: Date.now() });
    onPreview({ lat: poi.lat, lng: poi.lng, zoom: 15 });
    document.querySelector(".hero-map")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function closeWishPicker() {
    setWishPickerOpen(false);
    setWishPickerPoi(null);
  }

  function confirmWishPoi() {
    if (!wishPickerPoi) return;
    const id = poiId(wishPickerPoi);
    setPickedWishPois((current) => {
      const exists = current.some((item) => poiId(item) === id);
      return exists ? current.filter((item) => poiId(item) !== id) : [...current, wishPickerPoi];
    });
    closeWishPicker();
  }

  const routePreview = suggestionPreview;
  const routePreviewBusy = suggestionPreviewBusy;

  useEffect(() => {
    if (!routePreview?.knooppunten?.length) return;
    rememberNodes(...routePreview.knooppunten);
  }, [routePreview?.knooppunten]);

  const previewRefreshing =
    Boolean(previewKey) && routePreviewBusy && loadedPreviewKey !== previewKey;

  const selectedSuggestion = useMemo(
    () => suggestions.find((item) => item.id === selectedSuggestionId) || null,
    [suggestions, selectedSuggestionId],
  );

  function previewKnooppuntenFor(chain, isLoop) {
    const base = chain || [];
    if (!base.length) return [];
    return displayLoopNodes(base, isLoop);
  }

  const previewKnooppunten = useMemo(
    () => previewKnooppuntenFor(routePreview?.knooppunten, mode !== "punt"),
    [routePreview?.knooppunten, mode],
  );
  const suggestPreviewKnooppunten = useMemo(
    () =>
      previewKnooppuntenFor(
        suggestionPreview?.knooppunten,
        (selectedSuggestion?.mode || mode) !== "punt",
      ),
    [suggestionPreview?.knooppunten, selectedSuggestion?.mode, mode],
  );

  const wishSuggestions = useMemo(() => {
    if (buildMode === "manual") {
      return notes.trim() || debouncedNotes.trim() || activeInterests.length
        ? manualWishSuggestions
        : [];
    }
    if (buildMode === "auto" || buildMode === "suggest") return autoWishSuggestions;
    return [];
  }, [buildMode, notes, debouncedNotes, activeInterests.length, manualWishSuggestions, autoWishSuggestions]);

  const wishSummary =
    buildMode === "manual" ? manualWishSummary : autoWishSummary;

  const wishBusy =
    buildMode === "manual" ? manualWishBusy : autoWishBusy || routePreviewBusy;

  const panelError = useMemo(() => {
    const shown = [error, geoError].find(
      (msg) => msg && !isAbortError({ message: msg }),
    );
    return shown || "";
  }, [error, geoError]);

  useEffect(() => {
    // Nieuwe wens-tekst: focus resetten. Gekozen plekken wissen als wens leeg is.
    // Suggesties zelf laten we aan de fetch-effecten (profiel blijft zichtbaar zonder tekst).
    setFocusedWishId("");
    setFocusPulse(null);
    setManualWishError("");
    if (!notes.trim()) setPickedWishPois([]);
  }, [notes]);

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
    setStart(fallback);
    try {
      const hit = await reverseGeocode(next.lat, next.lng);
      if (reverseKeyRef.current !== key) return;
      const nice = hit?.label && !COORD_QUERY.test(hit.label) ? shortPlaceLabel(hit.label) : fallback;
      setStart(nice);
    } catch {
      if (reverseKeyRef.current === key) setStart(fallback);
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
    manualRouteIdsRef.current = [];
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
    setManualWishSuggestions([]);
    setManualWishSummary("");
    setManualWishBusy(false);
    setManualWishError("");
    setAutoWishSuggestions([]);
    setAutoWishSummary("");
    setAutoWishBusy(false);
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
      manualRouteIdsRef.current = [];
      reverseKeyRef.current = "";
    }

    setBuildMode(nextMode);
    if (nextMode === "manual") {
      setMode("punt");
    }
    if (nextMode === "auto") {
      setMode("lus");
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
    if (buildMode === "suggest") return;
    // Altijd knooppunten laden rond de gezochte stad (ook vóór startkeuze).
    setViewFocus({ lat, lng });
    setLocateTick((tick) => tick + 1);
    if (startChoice !== "map") return;
    setSelectedIds([]);
    setDraft(null);
    const placeLabel = label ? shortPlaceLabel(label) : undefined;
    await setFromCoords({ lat, lng }, "map", placeLabel);
  }

  async function chooseMyLocation() {
    setStartChoice("gps");
    setGeoBusy(true);
    setGeoError("");
    setSelectedIds([]);
    setNodeCatalog({});
    setDraft(null);
    try {
      const next = await getBrowserLocation();
      setHere(next);
      setViewFocus({ lat: next.lat, lng: next.lng });
      setOrigin({ lat: next.lat, lng: next.lng, source: "gps" });
      onPreview({ lat: next.lat, lng: next.lng, zoom: 14 });
      setStart("Jouw huidige locatie");
      setLocateTick((tick) => tick + 1);
      // Adreslabel op de achtergrond — reverse mag GPS nooit blokkeren.
      const key = `${next.lat.toFixed(4)},${next.lng.toFixed(4)}`;
      reverseKeyRef.current = key;
      void reverseGeocode(next.lat, next.lng)
        .then((hit) => {
          if (reverseKeyRef.current !== key) return;
          if (hit?.label && !COORD_QUERY.test(hit.label)) {
            setStart(shortPlaceLabel(hit.label));
          }
        })
        .catch(() => {});
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
    // Gekozen knooppuntenroute behouden — kaartklik mag die niet wissen.
    if (buildMode === "manual" && selectedIds.length > 0) {
      setViewFocus({ lat: next.lat, lng: next.lng });
      return;
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

    // Optimistic magenta: meteen stub tekenen (buiten setState-updater).
    if (!selectedIds.includes(id) && selectedIds.length >= 1) {
      const from = nodeLookup.get(selectedIds[selectedIds.length - 1]);
      if (from) {
        const prev = draftRef.current;
        const stub = [
          [from.lat, from.lng],
          [node.lat, node.lng],
        ];
        setDraft({
          ...(prev || {
            distance_km: 0,
            duration_min: 1,
            knooppunten: [],
            knoop_chain: "",
            steps: [],
            reason: "",
            weather: null,
          }),
          geometry:
            prev?.geometry?.length > 1
              ? mergeStreetGeometries(prev.geometry, stub)
              : stub,
        });
        setDraftBusy(true);
      }
    }
  }

  function undoLastKnoop() {
    if (buildMode !== "manual" || selectedIds.length === 0) return;
    setSelectedIds((current) => {
      if (!current.length) return current;
      const next = current.slice(0, -1);
      const removedId = current[current.length - 1];
      if (origin?.source === "knoop" && current[0] === removedId) {
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
    });
  }

  function nodeVariant(node) {
    if (mode === "punt" && selectedNodes.length) {
      const first = selectedNodes[0];
      const last = selectedNodes[selectedNodes.length - 1];
      if (knoopMatches(first, node, 250) || matchingUserPick(node, [first])) return "start";
      if (
        selectedNodes.length >= 2 &&
        (knoopMatches(last, node, 250) || matchingUserPick(node, [last]))
      ) {
        return "end";
      }
    } else if (origin?.source === "knoop" && knoopMatches(origin, node, 80)) {
      return "start";
    }
    const id = nodeId(node);
    // Gekozen of op de route: altijd groen.
    if (
      selectedIdSet.has(id) ||
      matchingUserPick(node, selectedNodes) ||
      node.on_route ||
      knoopOnRoute(node, routeNodes) ||
      knoopOnGeometry(node, draft?.geometry || routePreview?.geometry)
    ) {
      return "picked";
    }
    if (buildMode === "auto" && startChoice === "map") {
      return "route";
    }
    return "idle";
  }

  function isSelectedNode(node, index = -1) {
    if (index >= 0 && userPickedIndexes.has(index)) return true;
    const id = nodeId(node);
    if (selectedIdSet.has(id)) return true;
    if (matchingUserPick(node, selectedNodes)) return true;
    return false;
  }

  function submit(event) {
    event.preventDefault();
    setGeoError("");
    onClearError?.();
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
    if (buildMode === "manual" && !(draft?.geometry?.length > 1)) {
      setGeoError(
        draftBusy
          ? "De magenta route wordt nog berekend. Even wachten en opnieuw proberen."
          : "Nog geen magenta route. Kies minstens twee knooppunten en wacht tot de lijn verschijnt.",
      );
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
    const planKnoopNodes =
      buildMode === "manual" && draft?.knooppunten?.length >= 2 ? draft.knooppunten : selectedNodes;
    onPlan({
      start: planStart,
      end: planEnd,
      mode,
      interests: tripInterests.length ? tripInterests : ["geschiedenis"],
      distance_km: distanceKm,
      duration_min: budgetMode === "time" && buildMode === "auto" ? Number(duration) : null,
      budget_mode: budgetMode,
      notes: buildMode === "auto" || buildMode === "suggest" || buildMode === "manual" ? notes : "",
      explanation_level: profile?.commentary || "normaal",
      profile: toApiProfile(profile),
      suggestion_id: buildMode === "suggest" ? selectedSuggestionId : null,
      knooppunten:
        buildMode === "manual"
          ? planKnoopNodes.slice(0, 40).map((node) => ({
              id: node.id || "",
              number: node.number,
              lat: node.lat,
              lng: node.lng,
              network: node.network || null,
              geoid: node.geoid ?? null,
            }))
          : [],
      poi_picks: pickedWishPois.map((poi) => ({
        id: poi.id,
        name: poi.name,
        lat: poi.lat,
        lng: poi.lng,
        kind: poi.kind,
        kind_label: poi.kind_label || null,
        interest: poi.interest || "geschiedenis",
      })),
      route_geometry:
        buildMode === "manual" && draft?.geometry?.length > 1 ? draft.geometry : [],
      route_duration_min:
        buildMode === "manual" && draft?.duration_min ? Math.round(draft.duration_min) : null,
      local_manual: buildMode === "manual" && draft?.geometry?.length > 1,
      wish_suggestions: buildMode === "manual" ? wishSuggestions : [],
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

          {geoError && !isAbortError({ message: geoError }) && <div className="error">{geoError}</div>}

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
                {previewRefreshing
                  ? "Knooppuntenroute wordt berekend..."
                  : `${suggestPreviewKnooppunten.length} knooppunten in volgorde`}
              </p>
              {suggestPreviewKnooppunten.length > 0 && (
                <ol className="picked-list route-knoop-list" key={loadedPreviewKey || previewKey}>
                  {suggestPreviewKnooppunten.map((node, index) => (
                    <li key={`${nodeId(node)}-${index}`}>
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
              )}
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
                    : "Nog geen knooppunten in beeld. Zoom in of kies een startpositie."}
            </p>
            {routeNodes.length ? (
              <ol className="picked-list route-knoop-list">
                {routeNodes.map((node, index) => {
                  const picked = isSelectedNode(node, index);
                  return (
                    <li key={`${nodeId(node)}-${index}`} className={picked ? "picked-stop" : "via-stop"}>
                      <span className="num">{index + 1}</span>
                      <span>
                        <strong>Knooppunt {node.number}</strong>
                        {picked && mode === "punt" && selectedNodes.length && knoopMatches(selectedNodes[0], node, 250) && (
                          <small> · A</small>
                        )}
                        {picked &&
                          mode === "punt" &&
                          selectedNodes.length >= 2 &&
                          knoopMatches(selectedNodes[selectedNodes.length - 1], node, 250) && (
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
              <button
                type="button"
                className="ghost-link"
                onClick={() => {
                  setSelectedIds([]);
                  setDraft(null);
                  setDraftBusy(false);
                  manualRouteIdsRef.current = [];
                  legCacheRef.current.clear();
                }}
              >
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
                <div className="editor draft-box">
                  <strong>Te volgen knooppunten</strong>
                  <p className="sources" style={{ margin: "6px 0 8px" }}>
                    {previewRefreshing
                      ? "Knooppuntenroute wordt berekend..."
                      : `${previewKnooppunten.length} knooppunten · voorbeeld ${routePreview.distance_km} km`}
                  </p>
                  <ol className="picked-list route-knoop-list" key={loadedPreviewKey || previewKey}>
                    {previewKnooppunten.map((node, index) => (
                      <li key={`${nodeId(node)}-${index}`}>
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
                </div>
              )}
            </>
          )}

          {(buildMode === "auto" || buildMode === "manual") && (
          <label>
            Extra wens — cafés, kastelen, musea, natuur, …
            <textarea
              rows="2"
              placeholder="Bijvoorbeeld: cafés, kastelen, musea, langs het water, markten..."
              value={notes}
              onChange={(event) => setNotes(event.target.value)}
            />
          </label>
          )}
          {buildMode === "manual" && selectedNodes.length < 2 && (
            <p className="sources" style={{ margin: "0 0 12px" }}>
              Kies minstens twee knooppunten op de kaart. Daarna verschijnen hier plekken op basis van je
              profiel en Extra wens.
            </p>
          )}
          {buildMode === "manual" &&
            selectedNodes.length >= 2 &&
            draftBusy &&
            !wishSuggestions.length && (
            <p className="sources" style={{ margin: "0 0 12px" }}>
              Magenta-route wordt berekend — suggesties volgen meteen daarna…
            </p>
          )}

          {(buildMode === "auto" ||
            buildMode === "manual" ||
            (buildMode === "suggest" && selectedSuggestion)) &&
            wishSuggestions.length > 0 && (
            <div className="poi-suggest-section" id="suggestieoverzicht">
              <strong>Suggestieoverzicht</strong>
              <p className="sources" style={{ margin: "6px 0 8px" }}>
                {wishSummary ||
                  "Plekken langs je route op basis van je extra wens én je profiel. Klik om er een toe te voegen."}
              </p>
              <div className="poi-suggest-grid">
                {wishSuggestions.map((item) => {
                  const id = poiId(item);
                  const picked = pickedWishPois.some((poi) => poiId(poi) === id);
                  const glyph = wishPoiSvg(item.interest, item.kind_label || item.kind, item.name, 22);
                  return (
                    <button
                      key={id}
                      type="button"
                      className={`poi-suggest-tile ${picked ? "on" : ""} ${focusedWishId === id ? "focus" : ""}`}
                      onPointerUp={(event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        openWishPicker(item);
                      }}
                    >
                      <span
                        className="poi-suggest-glyph"
                        aria-hidden="true"
                        dangerouslySetInnerHTML={{ __html: glyph }}
                      />
                      <span className="poi-suggest-kind">{item.kind_label || item.kind}</span>
                      <strong>{item.name}</strong>
                      {item.hint && <small className="poi-suggest-note">{item.hint}</small>}
                      {item.on_route && !picked && !item.hint && (
                        <small className="poi-suggest-note">langs route</small>
                      )}
                    </button>
                  );
                })}
              </div>
              {pickedWishPois.length > 0 && (
                <p className="sources" style={{ margin: "8px 0 0" }}>
                  {pickedWishPois.length} plek{pickedWishPois.length === 1 ? "" : "ken"} toegevoegd
                  {previewRefreshing || wishBusy || draftBusy ? " — route wordt aangepast…" : " aan je route."}
                </p>
              )}
            </div>
          )}

          {(buildMode === "auto" || buildMode === "manual" || (buildMode === "suggest" && selectedSuggestion)) &&
            (notes.trim() || activeInterests.length > 0) &&
            wishBusy &&
            !wishSuggestions.length && (
            <p className="sources" style={{ margin: "0 0 12px" }}>
              {notes.trim()
                ? "Plekken voor je wens worden gezocht…"
                : "Suggesties op basis van je profiel worden geladen…"}
            </p>
          )}

          {buildMode === "auto" &&
            !routePreviewBusy &&
            !autoWishBusy &&
            !wishSuggestions.length &&
            routePreview?.geometry?.length > 1 &&
            (notes.trim() || activeInterests.length > 0) && (
            <p className="sources" style={{ margin: "0 0 12px" }}>
              {notes.trim()
                ? "Geen passende plekken gevonden voor je wens. Probeer een andere formulering."
                : "Nog geen profielsuggesties gevonden rond deze route."}
            </p>
          )}

          {buildMode === "manual" &&
            (notes.trim() || activeInterests.length > 0) &&
            !manualWishBusy &&
            draft?.geometry?.length > 1 &&
            !wishSuggestions.length && (
            <p className="sources" style={{ margin: "0 0 12px" }}>
              {manualWishError
                ? `Zoeken mislukt: ${manualWishError}`
                : notes.trim()
                  ? "Geen passende plekken gevonden voor je wens. Probeer “café” of “museum”, of even opnieuw zoeken."
                  : "Nog geen profielsuggesties gevonden rond deze route."}
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
          {panelError && <div className="error">{panelError}</div>}

          <button
            className="submit"
            type="submit"
            form="plan-form"
            disabled={
              busy ||
              draftBusy ||
              (buildMode === "suggest" && !selectedSuggestion) ||
              ((buildMode === "manual" || buildMode === "auto") && (!startChoice || !origin)) ||
              (buildMode === "manual" && !(draft?.geometry?.length > 1))
            }
          >
            {busy
              ? buildMode === "suggest"
                ? "Route wordt samengesteld..."
                : buildMode === "auto"
                  ? "Je tocht wordt samengesteld..."
                  : "Je knooppuntenroute wordt gepland..."
              : draftBusy && buildMode === "manual"
                ? "Magenta route wordt berekend..."
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
        {!mapBooted || !mapSeed ? (
          <div className="map-boot-pending" role="status" aria-live="polite">
            Locatie ophalen…
          </div>
        ) : (
        <>
        <MapContainer
          center={[mapSeed.lat, mapSeed.lng]}
          zoom={zoom}
          attributionControl
          zoomControl={false}
        >
          <TileLayer attribution={MAP_TILE.attribution} url={MAP_TILE.url} />
          <MapClick onPick={pickOnMap} />
          <MapResize />
          <MapReady onReady={setMap} />
          <MapPanFocus active={panFocusActive} onFocus={setViewFocus} />
          <HoldMapAfterUserPan />
          <MapZoomScale referenceZoom={zoom}>
          {(buildMode === "manual" && manualRouteLine?.positions?.length > 1 && (
            <RouteLine
              positions={manualRouteLine.positions}
              opacity={manualRouteLine.provisional ? 0.72 : 1}
              dashed={manualRouteLine.provisional}
            />
          ))}
          {/* Gekozen = groen; overgeslagen via-netwerk = rood. */}
          {(buildMode === "manual" && routeNodes.length > 0) && (
            <RouteChainKnoopMarkers
              nodes={routeNodes}
              geometry={draft?.geometry}
              pool={mapNodes}
            />
          )}
          {(buildMode === "suggest" || buildMode === "auto") && routePreview?.geometry?.length > 1 && (
            <RouteLine positions={routePreview.geometry} />
          )}
          {(buildMode === "auto" && previewKnooppunten.length > 0) && (
            <RouteChainKnoopMarkers nodes={previewKnooppunten} geometry={routePreview?.geometry} pool={mapNodes} />
          )}
          {(buildMode === "suggest" && suggestPreviewKnooppunten.length > 0) && (
            <RouteChainKnoopMarkers nodes={suggestPreviewKnooppunten} geometry={routePreview?.geometry} pool={mapNodes} />
          )}
          {wishSuggestions.length > 0 && (
            <WishRouteMarkers
              items={wishSuggestions}
              pickedIds={new Set(pickedWishPois.map((poi) => poiId(poi)))}
              focusedId={focusedWishId}
              onSelect={openWishPicker}
            />
          )}
          <FocusPulse
            position={focusPulse}
            token={focusPulse?.key}
            onDone={() => setFocusPulse(null)}
          />
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
            position={origin || (!startChoice ? here : null)}
            trigger={locateTick}
            zoom={buildMode === "suggest" ? 12 : 14}
          />
          <Recenter
            center={center}
            zoom={zoom}
            locked={
              // GPS mag de kaart niet meer overschrijven zodra er start of route is.
              Boolean(origin) ||
              (buildMode === "manual" && selectedIds.length > 0) ||
              (buildMode === "suggest" && !!selectedSuggestion) ||
              Boolean(
                (buildMode === "manual" && draft?.geometry?.length > 1) ||
                  ((buildMode === "suggest" || buildMode === "auto") &&
                    routePreview?.geometry?.length > 1),
              ) ||
              !center
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
              nodes={previewKnooppunten}
              active={buildMode === "suggest" ? !!selectedSuggestion : !!origin}
              deferAfterInteraction
              fitKey={
                buildMode === "suggest"
                  ? selectedSuggestionId || ""
                  : `${origin?.lat},${origin?.lng}|${previewKey}`
              }
            />
          )}
          {buildMode === "manual" && (
            <FitPreview
              geometry={draft?.geometry}
              nodes={routeNodes}
              active={Boolean(draft?.geometry?.length > 1 && routeNodes.length > 0)}
              deferAfterInteraction
              fitKey={selectedKey}
            />
          )}
          </MapZoomScale>
        </MapContainer>
        <div className="map-overlay">
          <MapChrome
            map={map}
            onLocate={chooseMyLocation}
            onGoTo={goToSearchPlace}
            onUndo={buildMode === "manual" ? undoLastKnoop : null}
            undoDisabled={selectedIds.length === 0}
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
        </>
        )}
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

      {wishPickerOpen &&
        wishPickerPoi &&
        createPortal(
          <div className="mode-picker-backdrop" onClick={closeWishPicker}>
            <div
              className="mode-picker poi-picker"
              role="dialog"
              aria-modal="true"
              aria-labelledby="wish-picker-title"
              onClick={(event) => event.stopPropagation()}
            >
              <h2 id="wish-picker-title" className="mode-picker-title">
                <span
                  aria-hidden="true"
                  className="wish-picker-glyph"
                  dangerouslySetInnerHTML={{
                    __html: wishPoiSvg(
                      wishPickerPoi.interest,
                      wishPickerPoi.kind_label || wishPickerPoi.kind,
                      wishPickerPoi.name,
                      22,
                    ),
                  }}
                />
                {wishPickerPoi.name}
              </h2>
              <p className="sources" style={{ margin: "8px 0 0" }}>
                {wishPickerPoi.kind_label || wishPickerPoi.kind}
                {wishPickerPoi.hint ? ` · ${wishPickerPoi.hint}` : ""}
              </p>
              <p className="sources" style={{ margin: "10px 0 0" }}>
                {pickedWishPois.some((poi) => poiId(poi) === poiId(wishPickerPoi))
                  ? "Deze plek zit al in je route. Wil je ze verwijderen?"
                  : "Wil je deze plek opnemen in je route? De fietsroute wordt dan aangepast."}
              </p>
              <div className="poi-picker-actions">
                <button type="button" className="submit" onClick={confirmWishPoi}>
                  {pickedWishPois.some((poi) => poiId(poi) === poiId(wishPickerPoi))
                    ? "Ja, verwijderen"
                    : "Ja, opnemen in de route"}
                </button>
                <button type="button" className="ghost mode-picker-cancel" onClick={closeWishPicker}>
                  Nee, overslaan
                </button>
              </div>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}

function WishRouteMarkers({ items, pickedIds, focusedId, onSelect }) {
  return items.map((item) => {
    const picked = pickedIds.has(poiId(item));
    return (
      <Marker
        key={item.id}
        position={[item.lat, item.lng]}
        icon={wishPoiIcon({
          interest: item.interest,
          kind: item.kind_label || item.kind,
          name: item.name,
          focused: focusedId === item.id,
          selected: picked,
        })}
        zIndexOffset={picked ? 1500 : 1400}
        eventHandlers={{
          click: (event) => {
            if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
            onSelect(item);
          },
        }}
      >
        <Popup>
          <strong>{item.name}</strong>
          <p style={{ margin: "6px 0 0" }}>
            {item.kind_label || item.kind}
            {picked ? " · toegevoegd aan route" : ""}
          </p>
        </Popup>
      </Marker>
    );
  });
}

function nearestOnGeometryNeighbors(nodes, index, geometry) {
  let prev = null;
  for (let j = index - 1; j >= 0; j -= 1) {
    if (knoopOnGeometry(nodes[j], geometry, 900)) {
      prev = nodes[j];
      break;
    }
  }
  let nxt = null;
  for (let j = index + 1; j < nodes.length; j += 1) {
    if (knoopOnGeometry(nodes[j], geometry, 900)) {
      nxt = nodes[j];
      break;
    }
  }
  return { prev, nxt };
}

function corridor76Position(geometry, pool) {
  if (!geometry?.length || geometry.length < 2) return null;
  const ref92 = (pool || []).find((n) => Number(n.geoid) === 5440047 || String(n.number) === "92");
  const ref83 = (pool || []).find(
    (n) =>
      Number(n.geoid) === 5421306 ||
      (String(n.number) === "83" && knoopMatches(n, { lat: 50.9869, lng: 4.6409 }, 450)),
  );
  if (!ref92 || !ref83) return null;
  if (!knoopOnGeometry(ref92, geometry, 1400) || !knoopOnGeometry(ref83, geometry, 900)) return null;
  const i92 = geometryProgressIndex(Number(ref92.lat), Number(ref92.lng), geometry);
  const i83 = geometryProgressIndex(Number(ref83.lat), Number(ref83.lng), geometry);
  const midIdx = Math.round((i92 + i83) / 2);
  const pt = geometry[midIdx];
  if (!pt?.length) return null;
  return { lat: pt[0], lng: pt[1] };
}

function RouteChainKnoopMarkers({ nodes, geometry, pool }) {
  const { scale } = useMapZoom();
  const corridor76 = corridor76Position(geometry, [...(pool || []), ...(nodes || [])]);
  const has76 = (nodes || []).some((node) => String(node.number) === "76");
  if (!nodes?.length && !corridor76) return null;
  const markers = (nodes || []).map((node, index) => {
    let lat = Number(node.lat);
    let lng = Number(node.lng);
    if (String(node.number) === "76" && corridor76) {
      lat = corridor76.lat;
      lng = corridor76.lng;
    } else if (geometry?.length > 1 && !knoopOnGeometry(node, geometry, 150)) {
      const { prev, nxt } = nearestOnGeometryNeighbors(nodes, index, geometry);
      if (prev && nxt) {
        const prevIdx = geometryProgressIndex(Number(prev.lat), Number(prev.lng), geometry);
        const nextIdx = geometryProgressIndex(Number(nxt.lat), Number(nxt.lng), geometry);
        const midIdx = Math.round((prevIdx + nextIdx) / 2);
        const pt = geometry[midIdx];
        if (pt?.length >= 2) {
          lat = pt[0];
          lng = pt[1];
        }
      }
    }
    return (
      <Marker
        key={`route-chain-${nodeId(node)}-${index}`}
        position={[lat, lng]}
        icon={nodeIcon(node.number, "picked", scale)}
        zIndexOffset={1400}
      >
        <Popup>Knooppunt {node.number}</Popup>
      </Marker>
    );
  });
  if (!has76 && corridor76) {
    markers.push(
      <Marker
        key="route-chain-76-corridor"
        position={[corridor76.lat, corridor76.lng]}
        icon={nodeIcon("76", "picked", scale)}
        zIndexOffset={1500}
      >
        <Popup>Knooppunt 76</Popup>
      </Marker>,
    );
  }
  return markers;
}

function RoutePreviewKnoopMarkers({ nodes, geometry }) {
  return <RouteChainKnoopMarkers nodes={nodes} geometry={geometry} />;
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

function HoldMapAfterUserPan() {
  const map = useMap();
  useEffect(() => {
    const el = map.getContainer();
    const mark = () => noteMapUserInteraction();
    el.addEventListener("pointerdown", mark);
    el.addEventListener("wheel", mark, { passive: true });
    el.addEventListener("touchstart", mark, { passive: true });
    return () => {
      el.removeEventListener("pointerdown", mark);
      el.removeEventListener("wheel", mark);
      el.removeEventListener("touchstart", mark);
    };
  }, [map]);
  useMapEvents({
    dragstart() {
      noteMapUserInteraction();
    },
    zoomstart() {
      noteMapUserInteraction();
    },
  });
  return null;
}

function MapPanFocus({ active, delayMs = MAP_KNOOP_LOAD_DEBOUNCE_MS, onFocus }) {
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
    if (locked || !center || center[0] == null || center[1] == null) return;
    map.setView(center, zoom || map.getZoom());
  }, [center, locked, map, zoom]);
  return null;
}

function FocusLastSelected({ node, trigger }) {
  const map = useMap();
  const seen = useRef("");
  useEffect(() => {
    if (!node || !trigger) return;
    // Alleen bij een nieuwe selectie — niet opnieuw als coördinaten na routing wijzigen.
    if (trigger === seen.current) return;
    seen.current = trigger;
    noteMapUserInteraction();
    map.flyTo([node.lat, node.lng], map.getZoom(), { duration: 0.45 });
  }, [map, node, trigger]);
  return null;
}

function FitNodes({ origin }) {
  const map = useMap();
  const seen = useRef("");
  useEffect(() => {
    if (!origin) return;
    const key = `${origin.lat.toFixed(5)},${origin.lng.toFixed(5)}`;
    if (key === seen.current) return;
    seen.current = key;
    map.setView([origin.lat, origin.lng], Math.max(map.getZoom(), 14), { animate: true });
  }, [origin?.lat, origin?.lng, map]);
  return null;
}

function geometryFitKey(geometry) {
  if (!geometry?.length) return "";
  const first = geometry[0];
  const mid = geometry[Math.floor(geometry.length / 2)];
  const last = geometry[geometry.length - 1];
  return [
    geometry.length,
    first?.[0],
    first?.[1],
    mid?.[0],
    mid?.[1],
    last?.[0],
    last?.[1],
  ].join("|");
}

function FitPreview({
  geometry,
  nodes,
  active,
  deferAfterInteraction = false,
  fitKey = "",
}) {
  const map = useMap();
  const timerRef = useRef(null);
  const fittingRef = useRef(false);
  const fittedGeomRef = useRef("");
  const seenGeomRef = useRef("");
  const seenFitKeyRef = useRef("");
  const fitKeyRef = useRef(fitKey);
  const geometryRef = useRef(geometry);
  const nodesRef = useRef(nodes);
  const activeRef = useRef(active);
  fitKeyRef.current = fitKey;
  geometryRef.current = geometry;
  nodesRef.current = nodes;
  activeRef.current = active;

  const fitBounds = () => {
    if (!activeRef.current) return;
    const bounds = [];
    const geom = geometryRef.current;
    if (geom?.length) {
      for (const point of geom) {
        if (point?.length >= 2) bounds.push([point[0], point[1]]);
      }
    }
    for (const node of nodesRef.current || []) {
      const lat = Number(node?.lat);
      const lng = Number(node?.lng);
      if (Number.isFinite(lat) && Number.isFinite(lng)) bounds.push([lat, lng]);
    }
    if (!bounds.length) return;
    fittingRef.current = true;
    map.fitBounds(bounds, {
      paddingTopLeft: [56, 56],
      paddingBottomRight: [56, 110],
      maxZoom: 13,
    });
    const key = geometryFitKey(geom);
    fittedGeomRef.current = key;
    seenGeomRef.current = key;
    map.once("moveend", () => {
      fittingRef.current = false;
    });
  };

  const scheduleOverview = () => {
    clearTimeout(timerRef.current);
    if (!activeRef.current) return;
    if (!deferAfterInteraction) {
      fitBounds();
      return;
    }
    const waitMs = msUntilRouteOverview();
    if (waitMs > 0) {
      timerRef.current = setTimeout(() => {
        if (msUntilRouteOverview() > 0) {
          scheduleOverview();
          return;
        }
        fitBounds();
      }, waitMs);
      return;
    }
    fitBounds();
  };

  const scheduleRef = useRef(scheduleOverview);
  scheduleRef.current = scheduleOverview;

  const onInteract = () => {
    if (!deferAfterInteraction || fittingRef.current) return;
    noteMapUserInteraction();
    scheduleRef.current();
  };

  useMapEvents({
    dragstart: onInteract,
    zoomstart: onInteract,
  });

  useEffect(() => {
    if (!deferAfterInteraction) return undefined;
    const el = map.getContainer();
    el.addEventListener("pointerdown", onInteract);
    el.addEventListener("wheel", onInteract, { passive: true });
    return () => {
      el.removeEventListener("pointerdown", onInteract);
      el.removeEventListener("wheel", onInteract);
    };
  }, [map, deferAfterInteraction]);

  useEffect(() => {
    if (!active) {
      clearTimeout(timerRef.current);
      return undefined;
    }

    // Extra knoop gekozen: blijf lokaal; overzicht pas na 5s stilte + 15s.
    if (fitKey && fitKey !== seenFitKeyRef.current) {
      seenFitKeyRef.current = fitKey;
      if (deferAfterInteraction && fittedGeomRef.current) {
        noteMapUserInteraction();
        scheduleOverview();
        return () => clearTimeout(timerRef.current);
      }
    }

    const geomKey = geometryFitKey(geometry);
    if (geomKey && geomKey !== seenGeomRef.current) {
      seenGeomRef.current = geomKey;
      if (!fittedGeomRef.current) {
        // Eerste magenta route: meteen totaaloverzicht.
        fitBounds();
      } else if (deferAfterInteraction) {
        noteMapUserInteraction();
        scheduleOverview();
      } else {
        fitBounds();
      }
      return () => clearTimeout(timerRef.current);
    }

    if (deferAfterInteraction && mapLastInteractAt) {
      scheduleOverview();
    }
    return () => clearTimeout(timerRef.current);
  }, [active, geometry, nodes, map, deferAfterInteraction, fitKey]);

  return null;
}
