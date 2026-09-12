import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MapContainer, Marker, Popup, TileLayer, useMap, useMapEvents } from "react-leaflet";
import L from "leaflet";
import { fetchBikeLeg, fetchBikeNetwork, fetchIcoonrouteStretch, fetchKnooppunten, fetchRouteSuggestions, fetchWishSuggestions, isAbortError, reverseGeocode, reroute, warmBikeNetwork } from "../api.js";
import { createBikeNetworkStore, ingestBikeNetwork, legFromLocalNetwork } from "../bikeNetwork.js";
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
  routeLength,
} from "../geo.js";
import { planAutoNodeChain } from "../planNodeChain.js";
import { useDebounced } from "../hooks.js";
import { nodeIcon, startIcon, wishPoiSvg, wishPoiIcon, isWishSearchPoi, wishOriginClass } from "../icons.js";
import { filterPoisNearRoute, WISH_ROUTE_CORRIDOR_M } from "../wishLocal.js";
import { profileSummary, suggestedDistance, suggestedMinutes, toApiProfile, mergeInterests, interestLabels, THEMES } from "../profile.js";
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

/** Alleen echte netwerktrajecten — stubs/provisioneel afwijzen. */
function isOfficialLeg(leg) {
  if (!leg || leg.provisional || leg.stub) return false;
  const geom = leg?.geometry;
  // /api/bike-leg levert enkel WFS-netwerk; ook 2-punts officiële stukjes zijn geldig.
  return Array.isArray(geom) && geom.length >= 2;
}

/** Stabiele cache-key op geoid/nummer+coords — niet op wisselende feature-ids. */
function legCacheKey(from, to) {
  const part = (node) => {
    if (node?.geoid != null && String(node.geoid) !== "") return `g${node.geoid}`;
    return `n${node?.number}|${Number(node?.lat).toFixed(4)}|${Number(node?.lng).toFixed(4)}`;
  };
  return `net2|${part(from)}|${part(to)}`;
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

function applyLegDraft(legs, picked) {
  const geometry = legs.reduce(
    (acc, leg) => mergeStreetGeometries(acc, leg.geometry || []),
    [],
  );
  if (geometry.length < 2) {
    throw new Error("Geen fietsroute gevonden langs de knooppunten.");
  }
  const chain = [];
  for (const leg of legs) {
    const via = Array.isArray(leg.via_knooppunten) ? leg.via_knooppunten : [];
    for (const node of via) {
      if (!node) continue;
      if (chain.length && nodeId(chain[chain.length - 1]) === nodeId(node)) continue;
      if (
        chain.length &&
        String(chain[chain.length - 1].number) === String(node.number) &&
        Math.abs(Number(chain[chain.length - 1].lat) - Number(node.lat)) < 0.0003 &&
        Math.abs(Number(chain[chain.length - 1].lng) - Number(node.lng)) < 0.0003
      ) {
        continue;
      }
      chain.push(node);
    }
  }
  const knooppunten = (chain.length >= 2 ? chain : picked).map((node) => ({
    id: node.id || "",
    number: node.number,
    lat: node.lat,
    lng: node.lng,
    network: node.network || null,
    geoid: node.geoid ?? null,
    on_route: true,
  }));
  // Altijd de echte magentalijn: som van segmenten én lengte van de polyline.
  const fromLegs = legs.reduce((sum, leg) => sum + Number(leg.distance_km || 0), 0);
  const fromGeom = routeLength(geometry) / 1000;
  const distanceKm = Math.max(fromLegs, fromGeom);
  const durationFromLegs = legs.reduce((sum, leg) => sum + Number(leg.duration_min || 0), 0);
  return {
    geometry,
    distance_km: Number(distanceKm.toFixed(1)),
    duration_min: Math.max(
      1,
      Math.round(durationFromLegs || (distanceKm / 16) * 60),
    ),
    knooppunten,
    knoop_chain: knooppunten.map((node) => node.number).join(" → "),
    steps: [],
    reason: "",
    weather: null,
  };
}

const KNOWN_INTERESTS = new Set(THEMES.map((item) => item.id));

function sanitizeWishGeometry(geometry) {
  const out = [];
  for (const point of geometry || []) {
    const lat = Number(Array.isArray(point) ? point[0] : point?.lat);
    const lng = Number(Array.isArray(point) ? point[1] : point?.lng);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) continue;
    out.push([lat, lng]);
  }
  // Dichter bemonsteren: corridor-filter volgt de magenta lijn beter.
  return sampleGeometryAlongRoute(out, 80);
}

function sanitizeWishNodes(nodes) {
  return (nodes || [])
    .filter((node) => Number.isFinite(Number(node?.lat)) && Number.isFinite(Number(node?.lng)))
    .map((node) => ({
      id: String(node.id || ""),
      number: String(node.number ?? ""),
      lat: Number(node.lat),
      lng: Number(node.lng),
      network: node.network || null,
    }));
}

function notesWantHoreca(text) {
  return /caf[eéè]|taverne|tavern|herberg|koffie|pub|\bbar\b|brasserie|estaminet|bistro/i.test(
    String(text || ""),
  );
}

function isHorecaSuggestion(poi) {
  const blob = `${poi?.interest || ""} ${poi?.kind || ""} ${poi?.kind_label || ""} ${poi?.name || ""}`;
  return poi?.interest === "horeca" || notesWantHoreca(blob);
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
  const legInflightRef = useRef(new Map());
  const bikeNetworkRef = useRef(createBikeNetworkStore());
  const selectedIdsRef = useRef(selectedIds);
  const paintMagentaRef = useRef(() => ({ missing: 0 }));
  const wishQueryRef = useRef({ notes: "", interests: [] });
  const routeBuildGenRef = useRef(0);
  const wishFetchGenRef = useRef(0);
  const suggestLoadGenRef = useRef(0);
  const stretchCacheRef = useRef(new Map()); // routeId -> stretch
  draftRef.current = draft;
  selectedIdsRef.current = selectedIds;
  const [suggestions, setSuggestions] = useState([]);
  const [suggestionsBusy, setSuggestionsBusy] = useState(false);
  const [selectedSuggestionId, setSelectedSuggestionId] = useState("");
  const [manualWishSuggestions, setManualWishSuggestions] = useState([]);
  const [manualWishSummary, setManualWishSummary] = useState("");
  const [manualWishBusy, setManualWishBusy] = useState(false);
  const [manualWishError, setManualWishError] = useState("");
  const manualWishSuggestionsRef = useRef([]);
  manualWishSuggestionsRef.current = manualWishSuggestions;
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
  const selectedKeyRaw = selectedIds.join(ID_JOIN);
  const selectedKeyDebounced = useDebounced(selectedKeyRaw, 150);
  const usesOfficialDraft =
    buildMode === "manual" ||
    buildMode === "auto" ||
    (buildMode === "suggest" && Boolean(selectedSuggestionId));
  // Zelf kiezen / auto / Top 10 (na keuze): meteen per knooppunt.
  const selectedKey = usesOfficialDraft ? selectedKeyRaw : selectedKeyDebounced;
  // Wens-tekst apart debouncen: anders herstart elke letter de zoektocht.
  const debouncedNotes = useDebounced(notes, 900);
  // Café/taverne: niet 900ms wachten — anders blijven geschiedenis-tegels staan.
  const wishSearchNotes = notesWantHoreca(notes) ? notes.trim() : debouncedNotes.trim();
  // Wensen: minimale debounce — blauwe tegels meteen naast magenta.
  // Auto/Top 10: iets langer zodat de knooppuntenketen kan settelen i.p.v. herhaald annuleren.
  const wishSelectedKeyFast = useDebounced(selectedKey, 80);
  const wishSelectedKeySlow = useDebounced(selectedKey, 320);
  // Icoonroutes: zelfde snelle wensen als zelf kiezen (keten komt in één keer).
  const wishSelectedKey =
    buildMode === "auto" ? wishSelectedKeySlow : wishSelectedKeyFast;
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
        // Officiële draft (zelf/auto/Top 10): geen via-netwerk-knopen als “op route” markeren.
        usesOfficialDraft ? selectedNodes : routeNodes,
        selectedNodes,
        null,
      ),
    [usesOfficialDraft, nodes, routeNodes, selectedNodes],
  );

  const estimateKm = estimateRouteKm(origin, selectedNodes, mode !== "punt");
  const liveKm = draft?.distance_km ?? estimateKm;
  const liveMin = draft?.duration_min;

  // Magenta: alleen officiële netwerkgeometrie (geen stub/vogelvlucht).
  const manualRouteLine = useMemo(() => {
    if (!usesOfficialDraft || !(draft?.geometry?.length > 1)) return null;
    return { positions: draft.geometry, provisional: false };
  }, [usesOfficialDraft, draft?.geometry]);

  const nodeLookupRef = useRef(nodeLookup);
  nodeLookupRef.current = nodeLookup;

  function paintMagentaFromCache() {
    if (!usesOfficialDraft) return { missing: 0, drawn: 0 };
    const liveLookup = nodeLookupRef.current;
    const livePicked = selectedIdsRef.current.map((id) => liveLookup.get(id)).filter(Boolean);
    if (livePicked.length < 2) {
      setDraft(null);
      setDraftBusy(false);
      return { missing: 0, drawn: 0 };
    }

    // Alleen aaneengesloten officiële segmenten vanaf het begin — nooit vogelvlucht.
    const officialLegs = [];
    let missing = 0;
    let gap = false;
    for (let index = 0; index < livePicked.length - 1; index += 1) {
      const from = livePicked[index];
      const to = livePicked[index + 1];
      const cached = legCacheRef.current.get(legCacheKey(from, to));
      if (cached && isOfficialLeg(cached)) {
        if (!gap) officialLegs.push(cached);
      } else {
        gap = true;
        missing += 1;
      }
    }

    manualRouteIdsRef.current = livePicked.map((node) => nodeId(node));
    setDraftBusy(missing > 0);

    if (!officialLegs.length) {
      // Nog geen officieel traject: geen lijn tekenen (geen stub).
      setDraft((prev) =>
        prev
          ? {
              ...prev,
              geometry: [],
              distance_km: 0,
              duration_min: 1,
              knooppunten: livePicked.map((node) => ({
                id: node.id || "",
                number: node.number,
                lat: node.lat,
                lng: node.lng,
                network: node.network || null,
                geoid: node.geoid ?? null,
                on_route: true,
              })),
              knoop_chain: livePicked.map((node) => node.number).join(" → "),
            }
          : {
              geometry: [],
              distance_km: 0,
              duration_min: 1,
              knooppunten: livePicked.map((node) => ({
                id: node.id || "",
                number: node.number,
                lat: node.lat,
                lng: node.lng,
                network: node.network || null,
                geoid: node.geoid ?? null,
                on_route: true,
              })),
              knoop_chain: livePicked.map((node) => node.number).join(" → "),
              steps: [],
              reason: "",
              weather: null,
            },
      );
      return { missing, drawn: 0 };
    }

    try {
      setDraft(applyLegDraft(officialLegs, livePicked));
      if (missing === 0) setGeoError("");
    } catch {
      /* negeer tijdelijke paint-fouten */
    }
    return { missing, drawn: officialLegs.length };
  }
  paintMagentaRef.current = paintMagentaFromCache;

  function ensureOfficialLeg(from, to) {
    const key = legCacheKey(from, to);
    const cached = legCacheRef.current.get(key);
    if (cached && isOfficialLeg(cached)) return Promise.resolve(cached);

    // Instant: kleur traject uit al geladen netwerk (zoals fietsknooppunten.be).
    const local = legFromLocalNetwork(bikeNetworkRef.current, from, to);
    if (local && isOfficialLeg(local)) {
      legCacheRef.current.set(key, local);
      paintMagentaRef.current();
      return Promise.resolve(local);
    }

    const inflight = legInflightRef.current.get(key);
    if (inflight) return inflight;
    const request = fetchBikeLeg(from, to)
      .then((leg) => {
        if (!isOfficialLeg(leg)) {
          throw new Error("Geen officiële knooppuntenroute tussen deze knooppunten.");
        }
        const official = { ...leg, official: true };
        legCacheRef.current.set(key, official);
        legInflightRef.current.delete(key);
        paintMagentaRef.current();
        return official;
      })
      .catch((err) => {
        legInflightRef.current.delete(key);
        throw err;
      });
    legInflightRef.current.set(key, request);
    return request;
  }

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

  const panFocusActive =
    buildMode === "manual" ||
    buildMode === "auto" ||
    buildMode === "suggest";

  const nodeFocus = useMemo(() => {
    // Gekozen icoonroute: knooppunten rond route-start.
    if (buildMode === "suggest" && selectedSuggestionId && origin) {
      return { lat: origin.lat, lng: origin.lng };
    }
    // Zoek/pan-focus: knooppunten laden rond wat de gebruiker bekijkt.
    if (panFocusActive && viewFocus) {
      return viewFocus;
    }
    if (panFocusActive && lastSelectedNode) {
      return { lat: lastSelectedNode.lat, lng: lastSelectedNode.lng };
    }
    if (origin) return { lat: origin.lat, lng: origin.lng };
    if (startChoice === "map" && center?.[0] != null && center?.[1] != null) {
      return { lat: center[0], lng: center[1] };
    }
    if (here) return { lat: here.lat, lng: here.lng };
    return null;
  }, [
    buildMode,
    selectedSuggestionId,
    panFocusActive,
    lastSelectedNode,
    viewFocus,
    origin,
    startChoice,
    center,
    here,
  ]);

  // ~1 km grid: kleine pan/recenter mag de fetch niet steeds annuleren.
  // Top 10: ook suggestion-id in de key zodat wisselen van route knopen herlaadt.
  const nodeFocusBucket = useMemo(() => {
    if (!panFocusActive || !nodeFocus) return "";
    const base = `${(Math.round(nodeFocus.lat * 100) / 100).toFixed(2)},${(Math.round(nodeFocus.lng * 100) / 100).toFixed(2)}`;
    if (buildMode === "suggest" && selectedSuggestionId) {
      return `${base}|${selectedSuggestionId}`;
    }
    return base;
  }, [panFocusActive, nodeFocus?.lat, nodeFocus?.lng, buildMode, selectedSuggestionId]);
  const nodeFocusBucketDebounced = useDebounced(nodeFocusBucket, MAP_KNOOP_LOAD_DEBOUNCE_MS);
  const nodesLoadGenRef = useRef(0);

  useEffect(() => {
    if (!panFocusActive || !nodeFocusBucketDebounced) {
      setNodesBusy(false);
      return undefined;
    }
    const [coordPart] = nodeFocusBucketDebounced.split("|");
    const [latS, lngS] = coordPart.split(",");
    const lat = Number(latS);
    const lng = Number(lngS);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) {
      setNodesBusy(false);
      return undefined;
    }
    const loadId = ++nodesLoadGenRef.current;
    let cancelled = false;
    setNodesBusy(true);

    const suggestMode = buildMode === "suggest" && Boolean(selectedSuggestionId);
    const radius = suggestMode ? 18000 : 12000;
    const centers = [{ lat, lng }];
    if (suggestMode) {
      const suggestion = suggestions.find((item) => item.id === selectedSuggestionId);
      for (const loc of suggestion?.localities || []) {
        if (!Number.isFinite(Number(loc?.lat)) || !Number.isFinite(Number(loc?.lng))) continue;
        if (haversine({ lat, lng }, loc) > 40_000) continue;
        centers.push({ lat: Number(loc.lat), lng: Number(loc.lng) });
      }
    }

    Promise.all(centers.map((c) => fetchKnooppunten(c.lat, c.lng, radius)))
      .then((batches) => {
        if (cancelled || loadId !== nodesLoadGenRef.current) return;
        const byId = new Map();
        for (const batch of batches) {
          for (const node of batch || []) {
            if (!node) continue;
            byId.set(nodeId(node), node);
          }
        }
        const next = Array.from(byId.values());
        if (suggestMode && selectedIdsRef.current.length >= 2) {
          // Officiële icoonroute-keten behouden; viewport-knopen alleen bijmixen.
          if (next.length) rememberNodes(...next);
          setNodes((prev) => {
            const map = new Map();
            for (const node of prev || []) map.set(nodeId(node), node);
            for (const node of next) map.set(nodeId(node), node);
            return Array.from(map.values());
          });
        } else {
          setNodes(next);
          if (next.length) rememberNodes(...next);
        }
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

    // Trajecten voor viewport: magenta kleuren lokaal i.p.v. WFS per klik.
    Promise.all(centers.map((c) => fetchBikeNetwork(c.lat, c.lng, Math.min(radius, 16000))))
      .then((nets) => {
        if (cancelled || loadId !== nodesLoadGenRef.current) return;
        for (const net of nets) {
          if (net) ingestBikeNetwork(bikeNetworkRef.current, net);
        }
        const lookup = nodeLookupRef.current;
        const picked = selectedIdsRef.current.map((id) => lookup.get(id)).filter(Boolean);
        for (let index = 0; index < picked.length - 1; index += 1) {
          ensureOfficialLeg(picked[index], picked[index + 1]).catch(() => {});
        }
        paintMagentaRef.current();
      })
      .catch(() => {
        /* bike-leg blijft fallback */
      });
    return () => {
      cancelled = true;
    };
  }, [panFocusActive, nodeFocusBucketDebounced, buildMode, selectedSuggestionId, suggestions]);

  useEffect(() => {
    if (!usesOfficialDraft || !selectedKey) {
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

    const buildId = ++routeBuildGenRef.current;
    let cancelled = false;

    const collectMissing = () => {
      const pending = [];
      for (let index = 0; index < picked.length - 1; index += 1) {
        const from = picked[index];
        const to = picked[index + 1];
        const cached = legCacheRef.current.get(legCacheKey(from, to));
        if (!(cached && isOfficialLeg(cached))) pending.push({ from, to });
      }
      return pending;
    };

    setGeoError("");
    const missing = collectMissing();
    paintMagentaFromCache();

    if (!missing.length) {
      rememberNodes(...picked);
      setDraftBusy(false);
      return () => {
        cancelled = true;
      };
    }

    // Parallel per segment (start→tip). Elk klaar prefix-segment wordt meteen getekend.
    missing.forEach(({ from, to }) => {
      ensureOfficialLeg(from, to)
        .then(() => {
          if (cancelled || buildId !== routeBuildGenRef.current) return;
          const painted = paintMagentaFromCache();
          if (painted.missing === 0) rememberNodes(...picked);
        })
        .catch(() => {
          /* retry hieronder */
        });
    });

    const retryTimer = window.setTimeout(() => {
      if (cancelled || buildId !== routeBuildGenRef.current) return;
      const stillMissing = collectMissing();
      if (!stillMissing.length) {
        paintMagentaFromCache();
        rememberNodes(...picked);
        return;
      }
      stillMissing.forEach(({ from, to }) => {
        ensureOfficialLeg(from, to)
          .then(() => {
            if (cancelled || buildId !== routeBuildGenRef.current) return;
            paintMagentaFromCache();
          })
          .catch(() => {});
      });
      window.setTimeout(() => {
        if (cancelled || buildId !== routeBuildGenRef.current) return;
        const painted = paintMagentaFromCache();
        if (painted.missing === 0) {
          rememberNodes(...picked);
          return;
        }
        if (!draftRef.current?.geometry?.length) {
          setGeoError("Geen officiële knooppuntenroute tussen deze knooppunten.");
        } else {
          setGeoError("Sommige trajecten laden nog. Klik het laatste knooppunt opnieuw indien nodig.");
        }
      }, 800);
    }, 1500);

    return () => {
      cancelled = true;
      window.clearTimeout(retryTimer);
    };
  }, [usesOfficialDraft, selectedKey]);

  // Plan mijn tocht + Route Top 10: knooppuntenketen lokaal (zelfde magenta-pad als zelf kiezen).
  const networkChainKey = useMemo(() => {
    if (!origin) return "";
    if (buildMode === "auto") {
      const endMatch = typeof end === "string" ? end.match(COORD_QUERY) : null;
      const endPart = endMatch
        ? `${Number(endMatch[1]).toFixed(4)},${Number(endMatch[2]).toFixed(4)}`
        : mode === "punt"
          ? "open"
          : "lus";
      return [
        "auto",
        origin.lat.toFixed(4),
        origin.lng.toFixed(4),
        mode,
        endPart,
        previewDistanceKm,
        nodes.length >= 5 ? "ok" : `n${nodes.length}`,
      ].join("|");
    }
    // Top 10 / icoonroutes: keten komt van /api/icoonroute (geen lokale generator).
    return "";
  }, [
    buildMode,
    origin?.lat,
    origin?.lng,
    mode,
    end,
    previewDistanceKm,
    nodes.length,
  ]);

  useEffect(() => {
    if (!networkChainKey || !origin || nodes.length < 5) return undefined;
    if (buildMode !== "auto") return undefined;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      if (cancelled) return;
      const pool = Array.from(nodeLookupRef.current.values());
      const radiusM = Math.max(22_000, previewDistanceKm * 700);
      const nearby = pool.filter((node) => haversine(origin, node) <= radiusM);
      const usePool = nearby.length >= 5 ? nearby : pool;
      if (usePool.length < 5) return;

      const endMatch = typeof end === "string" ? end.match(COORD_QUERY) : null;
      const endPoint =
        mode === "punt" && endMatch
          ? { lat: Number(endMatch[1]), lng: Number(endMatch[2]) }
          : null;

      const chain = planAutoNodeChain(usePool, origin, previewDistanceKm, endPoint);
      if (cancelled || chain.length < 2) return;

      let ids = chain.map((node) => nodeId(node));
      if (mode !== "punt" && ids[0] !== ids[ids.length - 1]) {
        ids = [...ids, ids[0]];
      }
      rememberNodes(...chain);
      setSelectedIds((prev) => (prev.join(ID_JOIN) === ids.join(ID_JOIN) ? prev : ids));
    }, 180);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [buildMode, networkChainKey]);

  useEffect(() => {
    if (profile?.interests?.length) setInterests(profile.interests);
  }, [profile]);

  const activeInterests = profile?.interests?.length ? profile.interests : interests;
  wishQueryRef.current = {
    notes: wishSearchNotes,
    interests: mergeInterests(
      activeInterests.length > 0 ? activeInterests : ["geschiedenis"],
      profile?.horeca?.length ? ["horeca"] : [],
      notesWantHoreca(wishSearchNotes) ? ["horeca"] : [],
    ),
  };

  // Suggesties starten meteen bij 2 knooppunten (niet wachten tot magenta klaar is).
  // Café-wens: horeca erbij, profiel + Top 10-thema's blijven meegaan.
  const wishInterestKey = mergeInterests(
    activeInterests.length > 0 ? activeInterests : ["geschiedenis"],
    interests,
    profile?.horeca?.length ? ["horeca"] : [],
    notesWantHoreca(wishSearchNotes) ? ["horeca"] : [],
  ).join(",");

  useLayoutEffect(() => {
    if (!usesOfficialDraft || wishSelectedKey.split(ID_JOIN).filter(Boolean).length < 2) return;
    if (notesWantHoreca(notes) || activeInterests.length > 0) setManualWishBusy(true);
  }, [usesOfficialDraft, wishSelectedKey, notes, activeInterests.length]);

  // Extra wens “café”: bestaande horeca-tegels meteen paars (wens), niet blauw (profiel) laten staan.
  useEffect(() => {
    if (!usesOfficialDraft || !notesWantHoreca(notes)) return undefined;
    setManualWishSuggestions((prev) => {
      if (!prev.length) return prev;
      let changed = false;
      const next = prev.map((poi) => {
        const blob = `${poi.kind || ""} ${poi.kind_label || ""} ${poi.name || ""}`.toLowerCase();
        const horeca =
          poi.interest === "horeca" ||
          /cafe|café|taverne|pub|\bbar\b|herberg|bistro|restaurant/.test(blob);
        if (!horeca || poi.source === "wish") return poi;
        changed = true;
        return { ...poi, source: "wish", hint: "past bij je wens", interest: "horeca" };
      });
      return changed ? next : prev;
    });
    return undefined;
  }, [usesOfficialDraft, notes]);

  useEffect(() => {
    if (!usesOfficialDraft) {
      setManualWishSuggestions([]);
      setManualWishSummary("");
      setManualWishBusy(false);
      setManualWishError("");
      return undefined;
    }
    const lookup = nodeLookupRef.current;
    const pickedRaw = wishSelectedKey
      .split(ID_JOIN)
      .filter(Boolean)
      .map((id) => lookup.get(id))
      .filter(Boolean);
    // Lus sluit met startknoop: unieke knopen tellen voor “≥2”.
    const seenIds = new Set();
    const picked = [];
    for (const node of pickedRaw) {
      const id = nodeId(node);
      if (seenIds.has(id)) continue;
      seenIds.add(id);
      picked.push(node);
    }
    if (picked.length < 2) {
      // Auto/Top 10: tijdelijk gat tijdens herplannen — tegels niet wissen.
      if (buildMode === "manual") {
        setManualWishSuggestions([]);
        setManualWishSummary("");
        setManualWishError("");
      }
      setManualWishBusy(false);
      return undefined;
    }
    // Altijd knooppuntenketen voor zoeken: magenta groeit nog (auto/Top 10) —
    // anders zoek je alleen op een kort prefix en blijven blauwe tegels weg.
    const geometry = sanitizeWishGeometry(picked.map((node) => [node.lat, node.lng]));
    const nodes = sanitizeWishNodes(picked);
    if (geometry.length < 2) {
      return undefined;
    }
    const wishInterests = wishInterestKey.split(",").filter((item) => KNOWN_INTERESTS.has(item));
    if (!wishInterests.length) wishInterests.push("geschiedenis");
    const fetchId = ++wishFetchGenRef.current;
    setManualWishBusy(true);
    setManualWishError("");

    (async () => {
      try {
        const data = await fetchWishSuggestions(
          {
            notes: wishSearchNotes,
            interests: wishInterests,
            geometry,
            nodes,
          },
          8_000,
        );
        const items = Array.isArray(data?.suggestions) ? data.suggestions : [];
        const summary = data?.wish_summary || "";
        const timedOut = Boolean(data?.timed_out);
        // Stale fetch: toch toepassen als er nog geen tegels zijn (voorkomt “blijft hangen”).
        if (fetchId !== wishFetchGenRef.current && manualWishSuggestionsRef.current.length) {
          return;
        }
        if (items.length) {
          setManualWishSuggestions(items);
          setManualWishSummary(summary);
          setManualWishError("");
        } else if (!manualWishSuggestionsRef.current.length) {
          setManualWishSummary(summary || "");
          setManualWishError(
            timedOut
              ? "Suggesties laden lukte niet. Tik Plan opnieuw of pas Extra wens aan."
              : wishSearchNotes
                ? "Geen plekken gevonden voor je wens. Probeer “café” of een andere term."
                : "Nog geen profielsuggesties gevonden rond deze route.",
          );
        }
      } catch (err) {
        if (fetchId !== wishFetchGenRef.current && manualWishSuggestionsRef.current.length) return;
        const timedOut = /duurde te lang/i.test(err?.message || "");
        if (!manualWishSuggestionsRef.current.length) {
          setManualWishError(
            timedOut
              ? "Suggesties laden lukte niet. Tik Plan opnieuw of pas Extra wens aan."
              : err?.message || "Wenssuggesties konden niet geladen worden.",
          );
        }
      } finally {
        if (fetchId === wishFetchGenRef.current || !manualWishSuggestionsRef.current.length) {
          setManualWishBusy(false);
        }
      }
    })();
    return undefined;
  }, [usesOfficialDraft, buildMode, wishSelectedKey, wishSearchNotes, wishInterestKey]);

  useEffect(() => {
    if (buildMode !== "suggest") return undefined;
    const lat = origin?.lat ?? here?.lat ?? 51.05;
    const lng = origin?.lng ?? here?.lng ?? 3.72;
    let cancelled = false;
    setSuggestionsBusy(true);
    // Alvast knooppunten + netwerk warmen rond jouw positie (zoals zelf kiezen).
    warmBikeNetwork({ lat, lng });
    fetchBikeNetwork(lat, lng, 16000)
      .then((net) => {
        if (!cancelled && net) ingestBikeNetwork(bikeNetworkRef.current, net);
      })
      .catch(() => {});
    fetchKnooppunten(lat, lng, 18000)
      .then((next) => {
        if (cancelled || !Array.isArray(next) || !next.length) return;
        rememberNodes(...next);
        setNodes((prev) => {
          if (selectedIdsRef.current.length >= 2) {
            const map = new Map((prev || []).map((n) => [nodeId(n), n]));
            for (const n of next) map.set(nodeId(n), n);
            return Array.from(map.values());
          }
          return next;
        });
      })
      .catch(() => {});

    fetchRouteSuggestions(lat, lng, activeInterests, getUsedRouteIds())
      .then((next) => {
        if (cancelled) return;
        setSuggestions(next);
        setSelectedSuggestionId((current) =>
          current && next.some((item) => item.id === current) ? current : "",
        );
        // Prefetch dichtstbijzijnde icoonroutes: stretch + netwerk klaar vóór klik.
        const focusLat = lat;
        const focusLng = lng;
        for (const item of (next || []).slice(0, 4)) {
          if (!item?.id) continue;
          warmBikeNetwork({ lat: item.lat, lng: item.lng });
          fetchBikeNetwork(item.lat, item.lng, 16000)
            .then((net) => {
              if (!cancelled && net) ingestBikeNetwork(bikeNetworkRef.current, net);
            })
            .catch(() => {});
          if (stretchCacheRef.current.has(item.id)) continue;
          fetchIcoonrouteStretch(item.id, focusLat, focusLng, item.distance_km || 50)
            .then((stretch) => {
              if (cancelled || !stretch?.knooppunten?.length) return;
              stretchCacheRef.current.set(item.id, stretch);
              if (Array.isArray(stretch.knooppunten)) {
                rememberNodes(...stretch.knooppunten);
              }
            })
            .catch(() => {});
        }
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

  function seedIcoonrouteLegs(chain, legs) {
    if (!Array.isArray(chain) || chain.length < 2) return 0;
    let seeded = 0;
    if (Array.isArray(legs) && legs.length) {
      for (let i = 0; i < chain.length - 1; i += 1) {
        const from = chain[i];
        const to = chain[i + 1];
        const leg = legs[i];
        if (!leg?.geometry?.length) continue;
        const official = {
          geometry: leg.geometry,
          distance_m: Number(leg.distance_m) || 0,
          duration_s: Number(leg.duration_s) || 60,
          official: true,
          via_knooppunten: [from, to],
        };
        legCacheRef.current.set(legCacheKey(from, to), official);
        seeded += 1;
      }
    }
    return seeded;
  }

  function applyIcoonrouteStretch(suggestion, stretch) {
    const chain = Array.isArray(stretch?.knooppunten) ? stretch.knooppunten : [];
    if (chain.length < 2) {
      throw new Error("Deze icoonroute leverde geen knooppuntenketen op.");
    }
    rememberNodes(...chain);
    setNodes((prev) => {
      const map = new Map((prev || []).map((n) => [nodeId(n), n]));
      for (const n of chain) map.set(nodeId(n), n);
      return Array.from(map.values());
    });
    const ids = chain.map((node) => nodeId(node));
    selectedIdsRef.current = ids;
    setSelectedIds(ids);
    seedIcoonrouteLegs(chain, stretch.legs);
    // Meteen magenta uit WFS-icoonroute-geometrie (zelfde gevoel als lokale legs).
    queueMicrotask(() => {
      const painted = paintMagentaFromCache();
      if (painted.drawn === 0 && stretch.geometry?.length > 1) {
        setDraft({
          geometry: stretch.geometry,
          distance_km: Number(stretch.distance_km) || estimateRouteKm(null, chain, false),
          duration_min: Math.max(1, Math.round(((Number(stretch.distance_km) || 50) / 16) * 60)),
          knooppunten: chain.map((node) => ({
            id: node.id || "",
            number: node.number,
            lat: node.lat,
            lng: node.lng,
            network: node.network || null,
            geoid: node.geoid ?? null,
            on_route: true,
          })),
          knoop_chain: chain.map((node) => node.number).join(" → "),
          steps: [],
          reason: "",
          weather: null,
        });
        setDraftBusy(false);
      }
      // Ontbrekende segmenten bijwerken via lokaal netwerk / bike-leg.
      for (let i = 0; i < chain.length - 1; i += 1) {
        ensureOfficialLeg(chain[i], chain[i + 1]).catch(() => {});
      }
    });

    const startNode = chain[0];
    setOrigin({
      lat: Number(stretch.lat) || startNode.lat,
      lng: Number(stretch.lng) || startNode.lng,
      source: "route",
    });
    setViewFocus({
      lat: Number(stretch.lat) || startNode.lat,
      lng: Number(stretch.lng) || startNode.lng,
    });
    setStart(stretch.start_label || stretch.start || suggestion.title);
    setMode(stretch.mode || "punt");
    const km = Math.max(8, Math.round(Number(stretch.distance_km) || suggestion.distance_km || 50));
    setDistance(km);
    setDuration(Math.round((km / 16) * 60));
    if (stretch.notes) setNotes(stretch.notes);
    if (Array.isArray(stretch.interests) && stretch.interests.length) {
      setInterests(stretch.interests);
    }
    setLocateTick((tick) => tick + 1);
    onPreview({
      lat: Number(stretch.lat) || startNode.lat,
      lng: Number(stretch.lng) || startNode.lng,
      zoom: 12,
    });
    warmBikeNetwork({
      lat: Number(stretch.lat) || startNode.lat,
      lng: Number(stretch.lng) || startNode.lng,
    });
    // Extra netwerk rond start/midden/eind voor snelle legs.
    const mid = chain[Math.floor(chain.length / 2)];
    const end = chain[chain.length - 1];
    for (const point of [startNode, mid, end]) {
      if (!point) continue;
      fetchBikeNetwork(point.lat, point.lng, 16000)
        .then((net) => {
          if (net) {
            ingestBikeNetwork(bikeNetworkRef.current, net);
            paintMagentaRef.current();
          }
        })
        .catch(() => {});
    }
  }

  async function applySuggestion(suggestion) {
    if (!Number.isFinite(suggestion.lat) || !Number.isFinite(suggestion.lng)) {
      setGeoError("Deze route heeft geen geldig startpunt. Probeer opnieuw te laden.");
      return;
    }
    const loadId = ++suggestLoadGenRef.current;
    setSelectedSuggestionId(suggestion.id);
    setStart(shortPlaceLabel(suggestion.start) || suggestion.start);
    setStartChoice("map");
    setEnd(suggestion.end || "");
    setMode(suggestion.mode || "punt");
    setInterests(suggestion.interests || []);
    setDistance(suggestion.distance_km || 50);
    setDuration(Math.round(((suggestion.distance_km || 50) / 16) * 60));
    setBudgetMode("distance");
    setNotes(suggestion.notes || "");
    setSelectedIds([]);
    selectedIdsRef.current = [];
    setDraft(null);
    setDraftBusy(true);
    setManualWishSuggestions([]);
    setManualWishSummary("");
    setManualWishError("");
    setGeoError("");
    setViewFocus({ lat: suggestion.lat, lng: suggestion.lng });
    // Behoud voorgewarmd netwerk/catalogus — niet wissen zoals vroeger.
    legInflightRef.current.clear();
    manualRouteIdsRef.current = [];

    const focusLat = Number.isFinite(here?.lat) ? here.lat : suggestion.lat;
    const focusLng = Number.isFinite(here?.lng) ? here.lng : suggestion.lng;
    setOrigin({ lat: suggestion.lat, lng: suggestion.lng, source: "route" });
    setLocateTick((tick) => tick + 1);
    onPreview({ lat: suggestion.lat, lng: suggestion.lng, zoom: 12 });
    warmBikeNetwork({ lat: suggestion.lat, lng: suggestion.lng });

    try {
      let stretch = stretchCacheRef.current.get(suggestion.id);
      if (!stretch?.knooppunten?.length) {
        stretch = await fetchIcoonrouteStretch(
          suggestion.id,
          focusLat,
          focusLng,
          suggestion.distance_km || 50,
        );
        if (stretch?.knooppunten?.length) {
          stretchCacheRef.current.set(suggestion.id, stretch);
        }
      }
      if (loadId !== suggestLoadGenRef.current) return;
      applyIcoonrouteStretch(suggestion, stretch);
      setGeoError("");
    } catch (err) {
      if (loadId !== suggestLoadGenRef.current) return;
      setGeoError(err?.message || "Icoonroute kon niet geladen worden.");
      setSelectedIds([]);
      selectedIdsRef.current = [];
      setDraft(null);
      setDraftBusy(false);
    }
  }

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

  const selectedSuggestion = useMemo(
    () => suggestions.find((item) => item.id === selectedSuggestionId) || null,
    [suggestions, selectedSuggestionId],
  );

  const wishSuggestions = useMemo(() => {
    const raw = usesOfficialDraft ? manualWishSuggestions : [];
    if (!raw.length) return [];
    const knoopGeom = selectedNodes.map((n) => [n.lat, n.lng]);
    const geom =
      draft?.geometry?.length > 1 && !draftBusy
        ? draft.geometry
        : knoopGeom.length >= 2
          ? knoopGeom
          : draft?.geometry || [];
    return filterPoisNearRoute(raw, geom, selectedNodes, WISH_ROUTE_CORRIDOR_M);
  }, [usesOfficialDraft, manualWishSuggestions, draft?.geometry, draftBusy, selectedNodes]);

  const wishSummary = manualWishSummary;
  const wishBusy = manualWishBusy;

  const panelError = useMemo(() => {
    const shown = [error, geoError].find(
      (msg) => msg && !isAbortError({ message: msg }),
    );
    return shown || "";
  }, [error, geoError]);

  useEffect(() => {
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
    setManualWishSuggestions([]);
    setManualWishSummary("");
    setManualWishBusy(false);
    setManualWishError("");
    cancelRouteModePicker();
    cancelStartPicker();

    if (nextMode === "suggest") {
      if (switching) {
        setStartChoice(null);
        setOrigin(null);
        setStart("");
        setSelectedIds([]);
        setDraft(null);
        setDraftBusy(false);
        stretchCacheRef.current.clear();
      }
      setBuildMode("suggest");
      // Knooppunten + netwerk al warmen rond GPS (vóór een route kiezen).
      const lat = here?.lat ?? 51.05;
      const lng = here?.lng ?? 3.72;
      warmBikeNetwork({ lat, lng });
      fetchBikeNetwork(lat, lng, 16000)
        .then((net) => {
          if (net) ingestBikeNetwork(bikeNetworkRef.current, net);
        })
        .catch(() => {});
      fetchKnooppunten(lat, lng, 18000)
        .then((next) => {
          if (!Array.isArray(next) || !next.length) return;
          rememberNodes(...next);
          setNodes(next);
        })
        .catch(() => {});
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
    warmBikeNetwork({ lat: node.lat, lng: node.lng, geoid: node.geoid });
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
        warmBikeNetwork({ lat: node.lat, lng: node.lng, geoid: node.geoid });
        return;
      }
      setStartFromKnoop(node);
      return;
    }

    setSelectedIds((current) => {
      if (current.includes(id)) {
        const next = current.filter((item) => item !== id);
        selectedIdsRef.current = next;
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
        queueMicrotask(() => paintMagentaFromCache());
        return next;
      }
      rememberNodes(node);
      const next = [...current, id];
      selectedIdsRef.current = next;
      return next;
    });

    // Prefetch buren van de tip + meteen officieel segment.
    warmBikeNetwork({ lat: node.lat, lng: node.lng, geoid: node.geoid });
    if (!selectedIds.includes(id) && selectedIds.length >= 1) {
      const from = nodeLookup.get(selectedIds[selectedIds.length - 1]);
      if (from) {
        setDraftBusy(true);
        ensureOfficialLeg(from, node).catch(() => {
          setGeoError("Geen officiële knooppuntenroute tussen deze knooppunten.");
        });
      }
    }
  }

  function undoLastKnoop() {
    if (buildMode !== "manual" || selectedIds.length === 0) return;
    setSelectedIds((current) => {
      if (!current.length) return current;
      const next = current.slice(0, -1);
      selectedIdsRef.current = next;
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
      queueMicrotask(() => paintMagentaFromCache());
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
      knoopOnGeometry(node, draft?.geometry)
    ) {
      return "picked";
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
    if (usesOfficialDraft && !selectedNodes.length) {
      setGeoError(
        buildMode === "auto"
          ? "Wacht tot er knooppunten op de route staan, of pas start/afstand aan."
          : "Kies minstens één knooppunt op de kaart.",
      );
      return;
    }
    if (buildMode === "manual" && mode === "punt" && selectedNodes.length < 2) {
      setGeoError("Kies minstens twee knooppunten: het eerste is A, het laatste is B.");
      return;
    }
    if ((buildMode === "auto" || buildMode === "suggest") && !(draft?.geometry?.length > 1)) {
      setGeoError(
        buildMode === "suggest"
          ? "Wacht tot de magenta knooppuntenroute klaar is, of kies een andere Top 10-route."
          : "Wacht tot de magenta knooppuntenroute klaar is.",
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
      usesOfficialDraft
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
      usesOfficialDraft && draft?.knooppunten?.length >= 2 ? draft.knooppunten : selectedNodes;
    onPlan({
      start: planStart,
      end: planEnd,
      mode,
      interests: mergeInterests(
        tripInterests.length ? tripInterests : ["geschiedenis"],
        notesWantHoreca(notes) ? ["horeca"] : [],
      ),
      distance_km: distanceKm,
      duration_min: budgetMode === "time" && buildMode === "auto" ? Number(duration) : null,
      budget_mode: budgetMode,
      notes: buildMode === "auto" || buildMode === "suggest" || buildMode === "manual" ? notes : "",
      explanation_level: profile?.commentary || "normaal",
      profile: toApiProfile(profile),
      suggestion_id: buildMode === "suggest" ? selectedSuggestionId : null,
      knooppunten:
        usesOfficialDraft
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
        usesOfficialDraft && draft?.geometry?.length > 1 ? draft.geometry : [],
      route_duration_min:
        usesOfficialDraft && draft?.duration_min ? Math.round(draft.duration_min) : null,
      local_manual: usesOfficialDraft && draft?.geometry?.length > 1,
      wish_suggestions: usesOfficialDraft
        ? wishSuggestions.length
          ? wishSuggestions
          : manualWishSuggestionsRef.current
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
            <span>Geef afstand of tijd. Knooppunten en magenta volgen het officiële netwerk — zelfde werkwijze als zelf kiezen.</span>
          </button>
          <button
            type="button"
            className={`choice-card ${buildMode === "suggest" ? "on" : ""}`}
            onClick={() => enterBuildMode("suggest")}
          >
            <strong>Icoonroutes</strong>
            <span>Officiële icoonroutes van Toerisme Vlaanderen — knooppunten en magenta op het echte traject.</span>
          </button>
        </div>

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
              <strong>Icoonroutes</strong>
              <p className="sources" style={{ margin: "6px 0 10px" }}>
                {suggestionsBusy
                  ? "Icoonroutes worden geladen…"
                  : "Officiële fiets-icoonroutes van Toerisme Vlaanderen. We tonen het stuk dichtst bij jou (~50 km)."}
              </p>
              <p className="sources" style={{ margin: "0 0 10px" }}>
                Bron: Toerisme Vlaanderen · geodata WFS (icoonroutes)
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
                <strong>Start:</strong> {start || selectedSuggestion.start}
              </p>
              <p className="sources" style={{ margin: "0 0 8px" }}>
                Officieel traject · Toerisme Vlaanderen icoonroute
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
                {draft?.geometry?.length > 1
                  ? ` · echt ${formatKm(liveKm)} via knooppunten`
                  : draftBusy
                    ? " · magenta groeit mee…"
                    : selectedNodes.length
                      ? " · route wordt samengesteld…"
                      : nodesBusy
                        ? " · knooppunten laden…"
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

          {buildMode === "suggest" && selectedSuggestion && (
            <div className="editor draft-box">
              <strong>Route via knooppunten</strong>
              <p className="sources" style={{ margin: "6px 0 8px" }}>
                {nodesBusy && selectedNodes.length < 2
                  ? "Knooppunten worden geladen…"
                  : draftBusy && !(draft?.geometry?.length > 1)
                    ? "Officiële trajecten groeien mee…"
                    : selectedNodes.length
                      ? `${routeNodes.length} knooppunten · ${formatKm(liveKm)}`
                      : nodes.length
                        ? "Route wordt samengesteld op het knooppuntennetwerk…"
                        : "Nog geen knooppunten in beeld rond deze start."}
              </p>
              {routeNodes.length ? (
                <ol className="picked-list route-knoop-list">
                  {routeNodes.map((node, index) => (
                    <li key={`${nodeId(node)}-${index}`} className="picked-stop">
                      <span className="num">{index + 1}</span>
                      <span>
                        <strong>Knooppunt {node.number}</strong>
                        {index > 0 &&
                          mode !== "punt" &&
                          index === routeNodes.length - 1 &&
                          knoopMatches(node, routeNodes[0], 80) && (
                            <small> · start</small>
                          )}
                      </span>
                    </li>
                  ))}
                </ol>
              ) : null}
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
                  legInflightRef.current.clear();
                }}
              >
                Selectie wissen
              </button>
            )}
          </div>
          )}

          {buildMode === "auto" && (
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
                    {draft?.geometry?.length > 1 ? ` · echt ${formatKm(liveKm)}` : ""}
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
              <div className="editor draft-box">
                <strong>Route via knooppunten</strong>
                <p className="sources" style={{ margin: "6px 0 8px" }}>
                  {!startChoice
                    ? "Kies eerst je startpunt."
                    : !origin
                      ? "Startpunt wordt gezet…"
                      : nodesBusy && selectedNodes.length < 2
                        ? "Knooppunten worden geladen…"
                        : draftBusy && !(draft?.geometry?.length > 1)
                          ? "Officiële trajecten groeien mee…"
                          : selectedNodes.length
                            ? `${routeNodes.length} knooppunten · ${formatKm(liveKm)}`
                            : nodes.length
                              ? "Route wordt samengesteld op het knooppuntennetwerk…"
                              : "Nog geen knooppunten in beeld. Zoom in of kies een startpositie."}
                </p>
                {routeNodes.length ? (
                  <ol className="picked-list route-knoop-list">
                    {routeNodes.map((node, index) => (
                      <li key={`${nodeId(node)}-${index}`} className="picked-stop">
                        <span className="num">{index + 1}</span>
                        <span>
                          <strong>Knooppunt {node.number}</strong>
                          {index > 0 &&
                            mode !== "punt" &&
                            index === routeNodes.length - 1 &&
                            knoopMatches(node, routeNodes[0], 80) && (
                              <small> · start</small>
                            )}
                        </span>
                      </li>
                    ))}
                  </ol>
                ) : null}
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
              </div>
            </>
          )}

          {(buildMode === "auto" || buildMode === "manual" || buildMode === "suggest") &&
            (buildMode !== "suggest" || selectedSuggestion) && (
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
          {usesOfficialDraft && selectedNodes.length < 2 && (
            <p className="sources" style={{ margin: "0 0 12px" }}>
              {buildMode === "manual"
                ? "Kies minstens twee knooppunten op de kaart. Daarna verschijnen hier plekken op basis van je profiel en Extra wens."
                : "Zodra er twee knooppunten op de route staan, verschijnen hier plekken op basis van je profiel en Extra wens."}
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
                  const fromWish = isWishSearchPoi(item);
                  const glyph = wishPoiSvg(item.interest, item.kind_label || item.kind, item.name, 22);
                  return (
                    <button
                      key={id}
                      type="button"
                      className={`poi-suggest-tile ${wishOriginClass(item)} ${picked ? "on" : ""} ${focusedWishId === id ? "focus" : ""}`}
                      onPointerUp={(event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        openWishPicker(item);
                      }}
                    >
                      <span
                        className={`poi-suggest-glyph ${wishOriginClass(item)}`}
                        aria-hidden="true"
                        dangerouslySetInnerHTML={{ __html: glyph }}
                      />
                      <span className="poi-suggest-kind">{item.kind_label || item.kind}</span>
                      <strong>{item.name}</strong>
                      <small className="poi-suggest-note">
                        {fromWish ? "Wens" : "Profiel"}
                        {item.hint && item.hint !== "past bij je wens" && item.hint !== "uit je profiel"
                          ? ` · ${item.hint}`
                          : ""}
                        {item.on_route && !picked ? " · langs route" : ""}
                      </small>
                      {picked ? <small className="poi-suggest-note">toegevoegd</small> : null}
                    </button>
                  );
                })}
              </div>
              {pickedWishPois.length > 0 && (
                <p className="sources" style={{ margin: "8px 0 0" }}>
                  {pickedWishPois.length} plek{pickedWishPois.length === 1 ? "" : "ken"} toegevoegd
                  {wishBusy
                    ? " — route wordt aangepast…"
                    : draftBusy
                      ? " — traject groeit mee…"
                      : " aan je route."}
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

          {(buildMode === "auto" || buildMode === "manual" || buildMode === "suggest") &&
            selectedNodes.length >= 2 &&
            (notes.trim() || activeInterests.length > 0 || interests?.length > 0) &&
            !manualWishBusy &&
            !wishSuggestions.length && (
            <p className="sources" style={{ margin: "0 0 12px" }}>
              {manualWishError ||
                (notes.trim()
                  ? "Geen passende plekken gevonden voor je wens. Probeer “café” of “museum”, of even opnieuw zoeken."
                  : "Nog geen profielsuggesties gevonden rond deze route.")}
            </p>
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
              (buildMode === "suggest" && !selectedSuggestion) ||
              ((buildMode === "manual" || buildMode === "auto") && (!startChoice || !origin)) ||
              (usesOfficialDraft && !(draft?.geometry?.length > 1))
            }
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
          {(usesOfficialDraft && manualRouteLine?.positions?.length > 1 && (
            <RouteLine
              positions={manualRouteLine.positions}
              opacity={1}
              dashed={false}
            />
          ))}
          {/* Gekozen = groen; overgeslagen via-netwerk = rood. */}
          {(usesOfficialDraft && routeNodes.length > 0) && (
            <RouteChainKnoopMarkers
              nodes={routeNodes}
              geometry={draft?.geometry}
              pool={mapNodes}
            />
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
          {usesOfficialDraft && (
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
          {origin && (origin.source === "map" || origin.source === "route") && (
            <Marker position={[origin.lat, origin.lng]} icon={startIcon} zIndexOffset={1100}>
              <Popup>{origin.source === "route" ? "Start Top 10-route" : "Zoekgebied / startpositie"}</Popup>
            </Marker>
          )}
          <HereMarker
            position={here && startChoice !== "map" ? here : null}
            accuracy={here && startChoice !== "map" ? here.accuracy : 0}
          />
          <MapFlyTo
            position={
              buildMode === "suggest" && origin
                ? origin
                : viewFocus || origin || (!startChoice ? here : null)
            }
            trigger={locateTick}
            zoom={buildMode === "suggest" ? 12 : viewFocus ? 12 : 14}
          />
          <Recenter
            center={center}
            zoom={zoom}
            locked={
              Boolean(origin) ||
              (usesOfficialDraft && selectedIds.length > 0) ||
              Boolean(usesOfficialDraft && draft?.geometry?.length > 1) ||
              !center
            }
          />
          {usesOfficialDraft && lastSelectedNode && (
            <FocusLastSelected node={lastSelectedNode} trigger={selectedIds.join("|")} />
          )}
          {buildMode === "manual" &&
            nodes.length > 0 &&
            selectedNodes.length === 0 &&
            origin &&
            origin.source === "map" && (
            <FitNodes origin={origin} />
          )}
          {usesOfficialDraft && (
            <FitPreview
              geometry={draft?.geometry}
              nodes={routeNodes}
              active={Boolean(draft?.geometry?.length > 1 && routeNodes.length > 0)}
              deferAfterInteraction
              fitKey={selectedKey || selectedSuggestionId || ""}
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
                ? draftBusy && !(draft?.geometry?.length > 1)
                  ? `${selectedSuggestion.title} · magenta groeit…`
                  : selectedNodes.length >= 2
                    ? `${selectedSuggestion.title} · ${routeNodes.length} knooppunten · ${formatKm(liveKm)}`
                    : `${selectedSuggestion.title} · knooppunten laden…`
                : "Kies een route uit de Top 10"
              : !startChoice
                ? "Kies je startpunt in het venster"
                : !origin
                  ? startChoice === "gps"
                    ? "Je locatie wordt opgehaald…"
                    : "Klik op de kaart of een knooppunt voor je start"
                  : draftBusy && !(draft?.geometry?.length > 1)
                    ? "Magenta groeit via het knooppuntennetwerk…"
                    : selectedNodes.length >= 2
                      ? `${routeNodes.length} knooppunten · ${formatKm(liveKm)}`
                      : "Route wordt samengesteld op knooppunten…"}
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
                  className={`wish-picker-glyph ${wishOriginClass(wishPickerPoi)}`}
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
          source: item.wish_source || item.source,
          hint: item.hint,
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
