import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MapContainer, Marker, Popup, TileLayer, useMap } from "react-leaflet";
import L from "leaflet";
import { askAbout, fetchStopSummary, fetchSurroundings, reroute } from "../api.js";
import {
  bearingDeg,
  compassLabel,
  displayLoopNodes,
  estimateRouteKm,
  formatDistance,
  formatDuration,
  formatKm,
  getBrowserLocation,
  haversine,
  interpolate,
  distanceAlongGeometry,
  ID_JOIN,
  knoopMatches,
  knoopOnRoute,
  knoopOnGeometry,
  listenOnce,
  mergeMapKnooppunten,
  nodeId,
  routeLength,
  speak,
  stopSpeaking,
  uniqueChainIds,
} from "../geo.js";
import { nodeIcon, wishPoiSvg, wishPoiIcon } from "../icons.js";
import { useDebounced } from "../hooks.js";
import { interestLabels } from "../profile.js";
import FocusPulse from "./FocusPulse.jsx";
import HereMarker from "./HereMarker.jsx";
import MapChrome from "./MapChrome.jsx";
import MapFlyTo from "./MapFlyTo.jsx";
import MapReady from "./MapReady.jsx";
import MapResize from "./MapResize.jsx";
import MapZoomScale, { useMapZoom } from "./MapZoomScale.jsx";
import RouteLine from "./RouteLine.jsx";
import { MAP_TILE } from "../mapTiles.js";
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
  const [map, setMap] = useState(null);
  const [mode, setMode] = useState("idle");
  const [position, setPosition] = useState({ lat: plan.start.lat, lng: plan.start.lng });
  const [gps, setGps] = useState(null);
  const [followGps, setFollowGps] = useState(false);
  const [locateTick, setLocateTick] = useState(0);
  const [focusTarget, setFocusTarget] = useState(null);
  const [focusPulse, setFocusPulse] = useState(null);
  const [focusedStop, setFocusedStop] = useState(null);
  const [stopPickerOpen, setStopPickerOpen] = useState(false);
  const [stopBlurbs, setStopBlurbs] = useState({});
  const [stopBlurbBusy, setStopBlurbBusy] = useState(false);
  const [stopBlurbOpen, setStopBlurbOpen] = useState(false);
  const [localityFactOpen, setLocalityFactOpen] = useState(false);
  const [guideExpanded, setGuideExpanded] = useState(false);
  const [routePoiIds, setRoutePoiIds] = useState([]);
  const stopPickerTimerRef = useRef(null);
  const fetchedBlurbIds = useRef(new Set());
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
  const [rideStartedAt, setRideStartedAt] = useState(null);
  const [nowTick, setNowTick] = useState(() => Date.now());
  const knoopPassedAtRef = useRef(new Map());
  const [surroundingsOn, setSurroundingsOn] = useState(() => plan.interaction !== "passief");
  const [surroundings, setSurroundings] = useState(null);
  const [surroundingsBusy, setSurroundingsBusy] = useState(false);
  const [surroundingsError, setSurroundingsError] = useState("");
  const [weatherOffer, setWeatherOffer] = useState(() => Boolean(plan.weather?.suggest_shorter));
  const [guideOpen, setGuideOpen] = useState(true);
  const guideOpenRef = useRef(true);
  guideOpenRef.current = guideOpen;
  const lastGps = useRef(null);
  const spoken = useRef(new Set());
  const spokenNav = useRef(new Set());
  const spokenLocality = useRef(new Set());
  const spokenKnoop = useRef(new Set());
  const stepIndexRef = useRef(0);
  const planRef = useRef(plan);
  const watchRef = useRef(null);
  const demoRef = useRef(null);
  const surroundingsFetchRef = useRef({ lat: 0, lng: 0, at: 0 });
  const surroundingsCellsRef = useRef(new Set());
  const surroundingsBusyRef = useRef(false);
  const surroundingsOnRef = useRef(surroundingsOn);
  const modeRef = useRef(mode);
  surroundingsOnRef.current = surroundingsOn;
  modeRef.current = mode;
  planRef.current = plan;

  const active = plan.stops.find((stop) => stop.id === activeId) || plan.stops[0];
  const suggestionStops = useMemo(() => {
    const wish = [];
    const rest = [];
    for (const stop of plan.stops || []) {
      if (stop.matches_wish) wish.push(stop);
      else rest.push(stop);
    }
    wish.sort((a, b) => Number(Boolean(a.on_route)) - Number(Boolean(b.on_route)));
    return [...wish, ...rest];
  }, [plan.stops]);
  const wishStops = useMemo(
    () => (plan.stops || []).filter((stop) => stop.matches_wish),
    [plan.stops],
  );
  const mapStops = useMemo(
    () => (plan.stops || []).filter((stop) => !stop.matches_wish),
    [plan.stops],
  );
  const allNodes = plan.all_knooppunten?.length ? plan.all_knooppunten : plan.knooppunten || [];
  const nodeLookup = useMemo(() => {
    const map = new Map();
    for (const node of allNodes) map.set(nodeId(node), node);
    for (const node of plan.knooppunten || []) map.set(nodeId(node), node);
    return map;
  }, [allNodes, plan.knooppunten]);
  const originalIds = useMemo(() => uniqueChainIds(plan.knooppunten).join(ID_JOIN), [plan.knooppunten]);
  const dirty = customIds.join(ID_JOIN) !== originalIds;
  const previewKey = useDebounced(customIds.join(ID_JOIN), 450);
  const selectedNodes = useMemo(
    () => customIds.map((id) => nodeLookup.get(id)).filter(Boolean),
    [customIds, nodeLookup],
  );
  const routeNodes = useMemo(() => {
    const base =
      dirty && draft?.knooppunten?.length ? draft.knooppunten : plan.knooppunten || selectedNodes;
    return displayLoopNodes(base, plan.mode !== "punt");
  }, [dirty, draft, plan.knooppunten, plan.mode, selectedNodes]);
  const mapNodes = useMemo(
    () =>
      mergeMapKnooppunten(
        allNodes,
        routeNodes,
        [],
        dirty && draft?.geometry?.length ? draft.geometry : plan.geometry,
      ),
    [allNodes, routeNodes, dirty, draft?.geometry, plan.geometry],
  );
  const liveKm = dirty
    ? draft?.distance_km ?? estimateRouteKm(plan.start, selectedNodes, plan.mode !== "punt")
    : plan.distance_km;
  const steps = plan.steps || [];
  const currentStep = steps[stepIndex] || null;
  const stepDistance = currentStep
    ? haversine(position, { lat: currentStep.lat, lng: currentStep.lng })
    : 0;
  const chat = chats.live || [];
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

  const rideGeometry = dirty && draft?.geometry?.length ? draft.geometry : plan.geometry;
  const knoopProgress = useMemo(() => {
    const chain = routeNodes || [];
    if (!chain.length) return null;
    const riderAlong = distanceAlongGeometry(position.lat, position.lng, rideGeometry);
    const withAlong = chain.map((node, index) => ({
      node,
      index,
      along: distanceAlongGeometry(Number(node.lat), Number(node.lng), rideGeometry),
      dist: haversine(position, { lat: node.lat, lng: node.lng }),
    }));
    let currentIndex = 0;
    for (let i = 0; i < withAlong.length; i += 1) {
      if (withAlong[i].along <= riderAlong + 80) currentIndex = i;
    }
    const near = withAlong.reduce((best, item) => (item.dist < best.dist ? item : best), withAlong[0]);
    if (near.dist < 80) currentIndex = near.index;
    const prev = currentIndex > 0 ? withAlong[currentIndex - 1] : null;
    const current = withAlong[currentIndex] || null;
    const next = currentIndex < withAlong.length - 1 ? withAlong[currentIndex + 1] : null;
    const elapsedMs = rideStartedAt ? Math.max(0, nowTick - rideStartedAt) : 0;
    const planMeters = Math.max(1, (plan.distance_km || 0) * 1000);
    const planMs = Math.max(1, (plan.duration_min || 1) * 60_000);
    const paceMsPerM =
      riderAlong > 40 && elapsedMs > 8_000 ? elapsedMs / riderAlong : planMs / planMeters;

    function card(item, role) {
      if (!item) return null;
      const id = nodeId(item.node);
      const passedAt = knoopPassedAtRef.current.get(id);
      let timeMs;
      if (role === "current" && rideStartedAt) timeMs = elapsedMs;
      else if (passedAt && rideStartedAt) timeMs = Math.max(0, passedAt - rideStartedAt);
      else timeMs = item.along * paceMsPerM;
      return {
        role,
        number: item.node.number,
        alongM: item.along,
        timeMs,
        distM: item.dist,
      };
    }

    return {
      prev: card(prev, "prev"),
      current: card(current, "current"),
      next: card(next, "next"),
      currentIndex,
      riderAlong,
      elapsedMs,
    };
  }, [
    nowTick,
    plan.distance_km,
    plan.duration_min,
    position,
    rideGeometry,
    rideStartedAt,
    routeNodes,
  ]);

  useEffect(() => {
    if (!knoopProgress?.current || mode === "idle") return;
    if (knoopProgress.current.distM < 100) {
      const id = nodeId(routeNodes[knoopProgress.currentIndex]);
      if (id && !knoopPassedAtRef.current.has(id)) {
        knoopPassedAtRef.current.set(id, Date.now());
      }
    }
  }, [knoopProgress, mode, routeNodes]);

  useEffect(() => {
    if (mode === "idle") return undefined;
    const id = window.setInterval(() => setNowTick(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [mode]);

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
  const guidePreview = useMemo(() => previewAndRest(guideText), [guideText]);

  function guideSpeak(text) {
    if (guideOpenRef.current) speak(text);
  }

  function closeGuide() {
    guideOpenRef.current = false;
    setGuideOpen(false);
    stopSpeaking();
  }

  function openGuide() {
    guideOpenRef.current = true;
    setGuideOpen(true);
  }

  useEffect(() => {
    guideSpeak(plan.intro);
  }, [plan.intro]);

  useEffect(() => {
    setGuideExpanded(false);
  }, [activeId, phase, guideText]);

  useEffect(() => {
    setStopBlurbOpen(false);
  }, [focusedStop?.id]);

  useEffect(() => {
    setLocalityFactOpen(false);
  }, [currentLocality?.name, currentLocality?.fact]);

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
    surroundingsCellsRef.current = new Set();
    surroundingsFetchRef.current = { lat: 0, lng: 0, at: 0 };
    setSurroundings(null);
    setSurroundingsError("");
    setPosition({ lat: plan.start.lat, lng: plan.start.lng });
    setDraft(null);
    setChats({});
    setGuideOpen(true);
    guideOpenRef.current = true;
  }, [plan.knoop_chain]);

  useEffect(() => {
    if (!dirty || !previewKey) {
      setDraft(null);
      setDraftBusy(false);
      return undefined;
    }
    const picked = previewKey
      .split(ID_JOIN)
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
      nodes: picked.map((node) => ({
        id: node.id || "",
        number: node.number,
        lat: node.lat,
        lng: node.lng,
        network: node.network || null,
        geoid: node.geoid ?? null,
        on_route: true,
      })),
      close_loop: plan.mode !== "punt",
      poi_picks: routePoiIds
        .map((id) => plan.stops.find((item) => item.id === id))
        .filter(Boolean)
        .map(stopToPoiPick),
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
  }, [dirty, nodeLookup, plan.mode, plan.start.lat, plan.start.lng, plan.stops, previewKey, routePoiIds]);

  useEffect(() => {
    return () => {
      stopTracking();
      stopSpeaking();
      clearTimeout(stopPickerTimerRef.current);
    };
  }, []);

  function closeStopPicker() {
    clearTimeout(stopPickerTimerRef.current);
    setStopPickerOpen(false);
    setFocusedStop(null);
  }

  function openStopSuggestion(stop) {
    if (!stop) return;
    clearTimeout(stopPickerTimerRef.current);
    setStopPickerOpen(false);
    setActiveId(stop.id);
    setPhase("arrived");
    setFollowGps(false);
    setFocusedStop(stop);
    setFocusTarget({ lat: stop.lat, lng: stop.lng, key: Date.now() });
    setFocusPulse({ lat: stop.lat, lng: stop.lng, key: Date.now() });
    guideSpeak(`${stop.arrived} ${stop.why || ""}`.trim());
    stopPickerTimerRef.current = setTimeout(() => {
      setStopPickerOpen(true);
    }, 2200);
  }

  useEffect(() => {
    if (!focusedStop?.id) return undefined;
    const existingFull = stopFullDescription(focusedStop);
    if (existingFull && !isGenericStopBlurb(existingFull, focusedStop)) {
      setStopBlurbs((current) =>
        current[focusedStop.id]
          ? current
          : { ...current, [focusedStop.id]: { text: existingFull, url: focusedStop.wikipedia_url || "" } },
      );
    }
    if (fetchedBlurbIds.current.has(focusedStop.id)) return undefined;
    fetchedBlurbIds.current.add(focusedStop.id);

    let cancelled = false;
    setStopBlurbBusy(true);
    fetchStopSummary({
      name: focusedStop.name,
      lat: focusedStop.lat,
      lng: focusedStop.lng,
      wikipedia_url: focusedStop.wikipedia_url,
      wikipedia: focusedStop.wikipedia,
      wikidata: focusedStop.wikidata,
      description: focusedStop.description,
      kind: focusedStop.kind,
    })
      .then((data) => {
        const text = (data?.summary || "").trim();
        if (cancelled || !text) return;
        setStopBlurbs((current) => {
          const prev = current[focusedStop.id];
          const prevText = typeof prev === "string" ? prev : prev?.text || "";
          const nextText = text.length >= prevText.length ? text : prevText;
          return {
            ...current,
            [focusedStop.id]: {
              text: nextText,
              url: data.url || (typeof prev === "object" ? prev?.url : "") || focusedStop.wikipedia_url || "",
            },
          };
        });
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setStopBlurbBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [focusedStop?.id, focusedStop?.name, focusedStop?.lat, focusedStop?.lng, focusedStop?.wikipedia_url, focusedStop?.wikipedia, focusedStop?.wikidata, focusedStop?.description, focusedStop?.kind]);

  function stopToPoiPick(stop) {
    return {
      id: stop.id,
      name: stop.name,
      lat: stop.lat,
      lng: stop.lng,
      kind: stop.kind || "plek",
      kind_label: stop.kind || null,
      interest: stop.interest || plan.interests?.[0] || "geschiedenis",
    };
  }

  async function includeFocusedStopInRoute() {
    if (!focusedStop) return;
    const stop = focusedStop;
    const selected = customIds.map((id) => nodeLookup.get(id)).filter(Boolean);
    const spine =
      selected.length > 0
        ? selected
        : (plan.knooppunten || []).filter((node, index, arr) => {
            if (!node) return false;
            if (index > 0 && nodeId(node) === nodeId(arr[0]) && index === arr.length - 1) return false;
            return true;
          });
    if (!spine.length) {
      setRerouteError("Geen knooppunten om de route langs deze plek te leggen.");
      closeStopPicker();
      return;
    }
    const nextPoiIds = routePoiIds.includes(stop.id) ? routePoiIds : [...routePoiIds, stop.id];
    const poiPicks = nextPoiIds
      .map((id) => plan.stops.find((item) => item.id === id) || (id === stop.id ? stop : null))
      .filter(Boolean)
      .map(stopToPoiPick);
    setRerouteBusy(true);
    setRerouteError("");
    try {
      const next = await reroute({
        start_lat: plan.start.lat,
        start_lng: plan.start.lng,
        nodes: spine.map((node) => ({
          id: node.id || "",
          number: node.number,
          lat: node.lat,
          lng: node.lng,
          network: node.network || null,
          geoid: node.geoid ?? null,
          on_route: true,
        })),
        close_loop: plan.mode !== "punt",
        poi_picks: poiPicks,
      });
      setRoutePoiIds(nextPoiIds);
      const routeIds = new Set((next.knooppunten || []).map((node) => nodeId(node)));
      onPlanChange({
        ...plan,
        ...next,
        knooppunten: next.knooppunten,
        all_knooppunten: (plan.all_knooppunten?.length ? plan.all_knooppunten : allNodes).map((node) => ({
          ...node,
          on_route: routeIds.has(nodeId(node)),
        })),
        route_reason: `Route via ${stop.name}`,
      });
      guideSpeak(`Oké, de route gaat langs ${stop.name}.`);
      closeStopPicker();
      // Na herberekening even wachten tot FitRoute klaar is, daarna terug inzoomen op de plek.
      setTimeout(() => {
        setFocusTarget({ lat: stop.lat, lng: stop.lng, key: Date.now() });
      }, 120);
    } catch (err) {
      setRerouteError(err.message);
    } finally {
      setRerouteBusy(false);
    }
  }

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
    setRideStartedAt(null);
    knoopPassedAtRef.current = new Map();
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
    maybeFetchSurroundings(here);
  }

  function surroundingsCellKey(here) {
    return `${here.lat.toFixed(3)}|${here.lng.toFixed(3)}`;
  }

  async function maybeFetchSurroundings(here) {
    if (!surroundingsOnRef.current || planRef.current.interaction === "passief") return;
    if (modeRef.current !== "live" && modeRef.current !== "demo") return;
    const cellKey = surroundingsCellKey(here);
    if (surroundingsCellsRef.current.has(cellKey) || surroundingsBusyRef.current) return;

    const last = surroundingsFetchRef.current;
    const moved = last.lat ? haversine(last, here) : Infinity;
    const elapsed = Date.now() - (last.at || 0);
    if (moved < 280 && elapsed < 90000) return;

    surroundingsBusyRef.current = true;
    setSurroundingsBusy(true);
    setSurroundingsError("");
    try {
      const data = await fetchSurroundings({
        lat: here.lat,
        lng: here.lng,
        interests: planRef.current.interests || [],
        explanation_level: planRef.current.explanation_level || "normaal",
        heading,
      });
      surroundingsFetchRef.current = { lat: here.lat, lng: here.lng, at: Date.now() };
      surroundingsCellsRef.current.add(cellKey);
      setSurroundings(data);
      if (data.summary) {
        guideSpeak(data.summary);
      }
    } catch (err) {
      setSurroundingsError(err.message);
    } finally {
      surroundingsBusyRef.current = false;
      setSurroundingsBusy(false);
    }
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
    knoopPassedAtRef.current = new Map();
    setRideStartedAt(Date.now());
    setNowTick(Date.now());
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
    knoopPassedAtRef.current = new Map();
    setRideStartedAt(Date.now());
    setNowTick(Date.now());
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
      guideSpeak(`${closest.arrived} ${closest.why || ""}`.trim());
    } else if (best < 700 && !spoken.current.has(`${closest.id}-near`)) {
      spoken.current.add(`${closest.id}-near`);
      setPhase("approaching");
      guideSpeak(closest.approaching);
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
      guideSpeak(step.instruction);
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
        nodes: selected.map((node) => ({
          id: node.id || "",
          number: node.number,
          lat: node.lat,
          lng: node.lng,
          network: node.network || null,
          geoid: node.geoid ?? null,
          on_route: true,
        })),
        close_loop: plan.mode !== "punt",
        poi_picks: routePoiIds
          .map((id) => plan.stops.find((item) => item.id === id))
          .filter(Boolean)
          .map(stopToPoiPick),
      });
      const routeIds = new Set((next.knooppunten || []).map((node) => nodeId(node)));
      const mergedAll = [...allNodes];
      for (const node of next.knooppunten || []) {
        if (!mergedAll.some((item) => nodeId(item) === nodeId(node))) mergedAll.push(node);
      }
      onPlanChange({
        ...plan,
        ...next,
        knooppunten: next.knooppunten,
        all_knooppunten: mergedAll.map((node) => ({
          ...node,
          on_route: routeIds.has(nodeId(node)),
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
        guideSpeak(`Knooppunt ${node.number} komt eraan.`);
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
        guideSpeak(`Je komt in ${place.name}${pop}.${fact}`);
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
      guideSpeak(next.reason || "Ik heb een aangepaste knooppuntenroute klaargezet.");
    } catch (err) {
      setRerouteError(err.message);
    } finally {
      setRerouteBusy(false);
    }
  }

  async function runAsk(asked) {
    const text = asked.trim();
    if (text.length < 2) return;
    setAskBusy(true);
    setAskError("");
    try {
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
        history: chat.slice(-6),
      });
      setChats((current) => ({
        ...current,
        live: [...(current.live || []), { q: text, a: data.answer }],
      }));
      setQuestion("");
      speak(data.answer);
    } catch (err) {
      setAskError(err.message);
    } finally {
      setAskBusy(false);
    }
  }

  async function submitQuestion(event) {
    event.preventDefault();
    if (plan.interaction === "passief") return;
    await runAsk(question);
  }

  async function askByVoice() {
    if (plan.interaction === "passief") return;
    setAskError("");
    setListening(true);
    try {
      const text = await listenOnce("nl-BE");
      setQuestion(text);
      setListening(false);
      await runAsk(text);
    } catch (err) {
      setAskError(err.message);
      setListening(false);
    }
  }

  function nodeVariant(node) {
    const id = nodeId(node);
    const geometry = dirty && draft?.geometry?.length ? draft.geometry : plan.geometry;
    if (
      customIds.includes(id) ||
      selectedNodes.some((picked) => knoopMatches(picked, node, 80)) ||
      node.on_route ||
      knoopOnRoute(node, routeNodes) ||
      knoopOnGeometry(node, geometry)
    ) {
      return "picked";
    }
    return "route";
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
              <>
                <p>
                  Je bent in {currentLocality.name}
                  {currentLocality.population ? ` (${currentLocality.population} inwoners)` : ""}.
                </p>
                {currentLocality.fact ? (
                  <ExpandableText
                    text={currentLocality.fact}
                    expanded={localityFactOpen}
                    onToggle={() => setLocalityFactOpen((open) => !open)}
                    className="locality-fact"
                    sentenceCount={2}
                  />
                ) : null}
              </>
            )}
          </div>
        )}
        {plan.route_reason && <p className="sources">{plan.route_reason}</p>}
        <div className="stats">
          <div className="stat">
            <span>{dirty ? "Voorlopige afstand" : "Afstand"}</span>
            <b>{typeof liveKm === "number" ? formatKm(liveKm) : `${plan.distance_km} km`}</b>
          </div>
          <div className="stat">
            <span>Rijtijd</span>
            <b>{dirty ? (draft?.duration_min ? formatDuration(draft.duration_min) : "…") : formatDuration(plan.duration_min)}</b>
          </div>
          <div className="stat">
            <span>Knooppunten</span>
            <b>{routeNodes.length}</b>
          </div>
        </div>
        <div className="actions">
          <button type="button" onClick={startLive}>
            Start route
          </button>
          {plan.interaction !== "passief" && (
            <button
              type="button"
              className={surroundingsOn ? "submit" : "ghost"}
              onClick={() => setSurroundingsOn((current) => !current)}
            >
              {surroundingsOn ? "Omgeving aan" : "Omgeving uit"}
            </button>
          )}
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
            ? surroundingsOn
              ? "GPS-navigatie aan: afslagen, uitleg en omgevingsinfo binnen 350 m worden voorgelezen."
              : "GPS-navigatie aan: afslagen en uitleg worden voorgelezen."
            : mode === "demo"
              ? "Demo: de fiets rijdt de route af met navigatie en uitleg."
              : "Start route voor GPS-begeleiding. De blauwe punt is jouw locatie."}
        </p>
        {plan.interaction !== "passief" && surroundingsOn && (
          <div className="context-card surroundings-card">
            <div className="kicker">Omgeving · 350 m</div>
            {surroundingsBusy && !surroundings?.summary ? (
              <p className="sources" style={{ margin: 0 }}>
                Omgeving wordt opgezocht…
              </p>
            ) : surroundings?.summary ? (
              <>
                {surroundings.place_name ? <strong>{surroundings.place_name}</strong> : null}
                <p>{surroundings.summary}</p>
                {surroundings.highlights?.length > 0 && (
                  <ul className="surroundings-highlights">
                    {surroundings.highlights.map((item) => (
                      <li key={`${item.name}-${item.distance_m}`}>
                        {item.name}
                        <small>
                          {" "}
                          · {interestLabels([item.interest])[0] || item.interest}
                          {typeof item.distance_m === "number" ? ` · ${item.distance_m} m` : ""}
                        </small>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            ) : (
              <p className="sources" style={{ margin: 0 }}>
                Start de route om live omgevingsinfo te krijgen.
              </p>
            )}
            {surroundingsError && <div className="error">{surroundingsError}</div>}
          </div>
        )}
        <div className="editor">
          <strong>Eigen route</strong>
          <p className="sources" style={{ margin: "6px 0 8px" }}>
            Grijze nummers liggen in de buurt. Klik om ze toe te voegen of te schrappen. Overgeslagen
            knooppunten worden via het netwerk aangevuld.
          </p>
          {routeNodes.length ? (
            <ol className="picked-list route-knoop-list">
              {routeNodes.map((node, index) => {
                const picked =
                  customIds.includes(nodeId(node)) ||
                  selectedNodes.some((item) => knoopMatches(item, node, 80));
                return (
                  <li key={`${nodeId(node)}-${index}`} className={picked ? "picked-stop" : "via-stop"}>
                    <span className="num">{index + 1}</span>
                    <span>
                      <strong>Knooppunt {node.number}</strong>
                      {picked ? <small> gekozen</small> : <small> · via netwerk</small>}
                      {plan.mode !== "punt" &&
                        index > 0 &&
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
          {draftBusy && dirty && <p className="sources" style={{ margin: 0 }}>Fietsroute wordt herberekend…</p>}
          {dirty && (
            <button className="submit" type="button" onClick={applyCustomRoute} disabled={rerouteBusy}>
              {rerouteBusy ? "Route wordt herberekend..." : "Neem deze knooppunten over"}
            </button>
          )}
        </div>
        {rerouteError && <div className="error">{rerouteError}</div>}
        {suggestionStops.length > 0 && (
          <div className="poi-suggest-section">
            <strong>Suggesties</strong>
            {plan.notes?.trim() ? (
              <p className="sources" style={{ margin: "6px 0 10px" }}>
                Op basis van je wens: {plan.notes.trim()}
              </p>
            ) : null}
            <div className="stop-list">
              {suggestionStops.map((stop, index) => (
                <button
                  key={stop.id}
                  type="button"
                  className={`stop ${stop.id === activeId ? "active" : ""} ${routePoiIds.includes(stop.id) ? "on-route" : ""} ${stop.matches_wish ? "wish" : ""} ${stop.matches_wish && !stop.on_route ? "wish-off-route" : ""}`}
                  onClick={() => openStopSuggestion(stop)}
                >
                  {stop.matches_wish ? (
                    <span
                      className="num wish-num"
                      aria-hidden="true"
                      dangerouslySetInnerHTML={{
                        __html: wishPoiSvg(
                          stop.interest,
                          stop.kind_label || stop.kind,
                          stop.name,
                          16,
                        ),
                      }}
                    />
                  ) : (
                    <span className="num" aria-hidden="true">
                      {index + 1}
                    </span>
                  )}
                  <span>
                    <strong>{stop.name}</strong>
                    <br />
                    <small>
                      {stop.matches_wish ? "Suggestie voor je wens · " : ""}
                      {interestLabels([stop.interest])[0] || stop.interest} · {stop.kind}
                      {stop.source ? ` · ${stop.source}` : ""}
                    </small>
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
        {plan.notes?.trim() && wishStops.some((stop) => stop.on_route) && suggestionStops.length === 0 && (
          <p className="sources" style={{ margin: "0 0 12px" }}>
            Je wens ligt op de route — zie het pictogram op de kaart.
          </p>
        )}
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
        {knoopProgress && (
          <div className="knoop-progress" aria-label="Knooppunten onderweg">
            <KnoopProgressCard label="Vorige" item={knoopProgress.prev} />
            <KnoopProgressCard label="Nu" item={knoopProgress.current} current />
            <KnoopProgressCard label="Volgende" item={knoopProgress.next} />
          </div>
        )}
        <MapContainer center={[plan.start.lat, plan.start.lng]} zoom={13} scrollWheelZoom zoomControl={false}>
          <TileLayer attribution={MAP_TILE.attribution} url={MAP_TILE.url} />
          <MapResize />
          <MapReady onReady={setMap} />
          <MapZoomScale referenceZoom={13}>
          <RouteLine positions={plan.geometry} opacity={dirty ? 0.45 : 1} />
          {dirty && draft?.geometry?.length > 1 && (
            <RouteLine positions={draft.geometry} color="#4f8f43" dashed />
          )}
          <RideKnoopMarkers nodes={mapNodes} nodeVariant={nodeVariant} onToggle={toggleNode} customIds={customIds} />
          {mapStops.map((stop, index) => (
            <Marker
              key={stop.id}
              position={[stop.lat, stop.lng]}
              icon={stopIcon(index + 1)}
              zIndexOffset={1000}
            >
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
          {wishStops.map((stop) => (
            <Marker
              key={`wish-${stop.id}`}
              position={[stop.lat, stop.lng]}
              icon={wishPoiIcon({
                interest: stop.interest,
                kind: stop.kind_label || stop.kind,
                name: stop.name,
                selected: Boolean(stop.on_route) || routePoiIds.includes(stop.id),
              })}
              zIndexOffset={1500}
              eventHandlers={{
                click: (event) => {
                  if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
                  openStopSuggestion(stop);
                },
              }}
            >
              <Popup>
                <strong>{stop.name}</strong>
                <p style={{ margin: "6px 0 0" }}>
                  Past bij je wens
                  {stop.on_route || routePoiIds.includes(stop.id) ? " · op je route" : ""}
                  {stop.kind_label || stop.kind ? ` · ${stop.kind_label || stop.kind}` : ""}
                </p>
              </Popup>
            </Marker>
          ))}
          <FocusPulse
            position={focusPulse}
            token={focusPulse?.key}
            onDone={() => setFocusPulse(null)}
          />
          {mode === "demo" && <Marker position={[position.lat, position.lng]} icon={bikeIcon} />}
          <HereMarker position={gps} accuracy={gps?.accuracy} />
          <MapFlyTo position={gps} trigger={locateTick} zoom={16} />
          <MapFlyTo
            position={focusTarget}
            trigger={focusTarget?.key || 0}
            zoom={15}
          />
          <FitRoute geometry={plan.geometry} nodes={plan.knooppunten} />
          <Follow position={mode === "demo" ? position : gps || position} enabled={mode === "demo" || followGps} />
          </MapZoomScale>
        </MapContainer>
        <div className="map-overlay">
          <MapChrome
            map={map}
            onLocate={showMyLocation}
            onGoTo={() => setFollowGps(false)}
            locateDisabled={locateBusy}
            locateBusy={locateBusy}
          />
        </div>
        {currentStep && mode !== "idle" && (
          <div className={`nav-banner${knoopProgress ? " with-knoop-progress" : ""}`}>
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
        {guideOpen ? (
          <div className="guide">
            <div className="guide-head">
              <div className="kicker">
                {phase === "intro"
                  ? "Je gids"
                  : active
                    ? `${active.name}${active.place_name ? ` · ${active.place_name}` : ""} · ${active.kind}`
                    : "Je gids"}
              </div>
              <button
                type="button"
                className="guide-close"
                onClick={closeGuide}
                aria-label="Gids sluiten"
              >
                ×
              </button>
            </div>
            <ExpandableText
              text={guideText}
              preview={guidePreview}
              expanded={guideExpanded}
              onToggle={() => setGuideExpanded((open) => !open)}
              className="guide-blurb"
            />
          </div>
        ) : (
          <button type="button" className="guide-reopen" onClick={openGuide}>
            Gids tonen
          </button>
        )}
      </section>

      {stopPickerOpen &&
        focusedStop &&
        createPortal(
          <div className="mode-picker-backdrop" onClick={closeStopPicker}>
            <div
              className="mode-picker poi-picker"
              role="dialog"
              aria-modal="true"
              aria-labelledby="ride-stop-picker-title"
              onClick={(event) => event.stopPropagation()}
            >
              <h2 id="ride-stop-picker-title" className="mode-picker-title">
                {focusedStop.name}
              </h2>
              <ExpandableText
                text={getStopPickerBlurb(focusedStop, stopBlurbs, stopBlurbBusy)}
                expanded={stopBlurbOpen}
                onToggle={() => setStopBlurbOpen((open) => !open)}
                className="stop-picker-blurb"
              />
              {focusedStop.matches_wish && plan.notes?.trim() ? (
                <p className="sources" style={{ margin: "10px 0 0" }}>
                  Past bij je wens: {plan.notes.trim()}
                </p>
              ) : null}
              <p className="sources" style={{ margin: "10px 0 0" }}>
                {focusedStop.on_route || routePoiIds.includes(focusedStop.id)
                  ? "Deze plek zit al in je route."
                  : "Wil je deze plek meenemen in je route? De fietsroute wordt dan automatisch aangepast."}
              </p>
              <div className="poi-picker-actions">
                {focusedStop.on_route || routePoiIds.includes(focusedStop.id) ? (
                  <button type="button" className="ghost mode-picker-cancel" onClick={closeStopPicker}>
                    Sluiten
                  </button>
                ) : (
                  <>
                    <button
                      type="button"
                      className="submit"
                      onClick={includeFocusedStopInRoute}
                      disabled={rerouteBusy}
                    >
                      {rerouteBusy ? "Route wordt aangepast..." : "Ja, opnemen in de route"}
                    </button>
                    <button type="button" className="ghost mode-picker-cancel" onClick={closeStopPicker}>
                      Nee, overslaan
                    </button>
                  </>
                )}
              </div>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}

function stopFullDescription(stop) {
  return (
    stop?.summary?.trim() ||
    stop?.description?.trim() ||
    stop?.arrived?.trim() ||
    stop?.local_fact?.trim() ||
    ""
  );
}

function truncateAtWord(text, maxChars) {
  const raw = (text || "").trim();
  if (raw.length <= maxChars) return raw;
  const cut = raw.slice(0, maxChars);
  const at = cut.lastIndexOf(" ");
  return `${(at > 40 ? cut.slice(0, at) : cut).trim()}…`;
}

function previewSentences(text, count = 2, maxChars = 240) {
  const raw = (text || "").trim();
  if (!raw) return { preview: "", rest: "" };
  if (raw.length <= maxChars) {
    const sentences = raw.match(/[^.!?]+[.!?]+/g) || [];
    if (sentences.length > count) {
      const preview = sentences.slice(0, count).join(" ").trim();
      return { preview, rest: raw.slice(preview.length).trim() };
    }
    return { preview: raw, rest: "" };
  }

  const sentences = raw.match(/[^.!?]+[.!?]+/g) || [];
  let preview = sentences.length ? sentences.slice(0, count).join(" ").trim() : raw;
  if (preview.length > maxChars || preview.length >= raw.length) {
    preview = truncateAtWord(raw, maxChars);
  } else if (sentences.length <= 1) {
    preview = truncateAtWord(raw, maxChars);
  }

  const plainLen = preview.replace(/…$/, "").trim().length;
  if (plainLen >= raw.length) return { preview: raw, rest: "" };
  return { preview, rest: raw };
}

function previewAndRest(text, limit = 160) {
  const raw = (text || "").trim();
  if (!raw) return { preview: "", rest: "" };
  if (raw.length <= limit) return { preview: raw, rest: "" };
  const sentences = raw.match(/[^.!?]+[.!?]+/g);
  const first = sentences?.[0]?.trim() || raw;
  if (first.length <= limit + 50 && first.length < raw.length) {
    return { preview: first, rest: raw.slice(first.length).trim() };
  }
  const cut = raw.slice(0, limit);
  const at = cut.lastIndexOf(" ");
  const preview = `${(at > 80 ? cut.slice(0, at) : cut).trim()}…`;
  return { preview, rest: raw };
}

function ExpandableText({ text, preview, expanded, onToggle, className, sentenceCount }) {
  const raw = (text || "").trim();
  const parts =
    preview ||
    (sentenceCount ? previewSentences(raw, sentenceCount) : previewAndRest(raw));
  const shown = expanded ? raw : parts.preview;
  const previewLen = (parts.preview || "").replace(/…$/, "").trim().length;
  const canExpand = raw.length > previewLen;
  return (
    <div className={className}>
      <p className={expanded ? "blurb-expanded" : undefined}>{shown || raw}</p>
      {canExpand && (
        <button type="button" className="ghost-link lees-meer" onClick={onToggle}>
          {expanded ? "Toon minder" : "Lees meer"}
        </button>
      )}
    </div>
  );
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function isGenericStopBlurb(text, stop) {
  if (!text?.trim()) return true;
  const name = escapeRegExp((stop?.name || "").trim());
  const kind = escapeRegExp((stop?.kind || "plek").trim());
  const patterns = [
    name ? new RegExp(`^${name} is een ${kind} langs je (fiets)?route\\.?$`, "i") : null,
    /past bij je interesse in/i,
    /langs je fietsroute/i,
    /langs je route/i,
    /bekend via OpenStreetMap/i,
    /Je nadert .+, een plek met een verhaal/i,
    /is een plek met een verhaal in Belgi/i,
  ].filter(Boolean);
  return patterns.some((pattern) => pattern.test(text.trim()));
}

function getStopPickerBlurb(stop, stopBlurbs, stopBlurbBusy) {
  const stored = stopBlurbs[stop?.id];
  const lookedUp = typeof stored === "string" ? stored : stored?.text;
  if (lookedUp) return lookedUp;
  const full = stopFullDescription(stop);
  if (full && !isGenericStopBlurb(full, stop)) return full;
  if (stopBlurbBusy) return "Beschrijving wordt opgehaald…";
  if (full) return full;
  const kind = (stop?.kind || "plek").toLowerCase();
  return `${stop.name} is een ${kind} langs je route.`;
}

function RideKnoopMarkers({ nodes, nodeVariant, onToggle, customIds }) {
  const { scale, showKnoopMarkers } = useMapZoom();
  if (!showKnoopMarkers) return null;
  return nodes.map((node) => {
    const variant = nodeVariant(node);
    return (
      <Marker
        key={nodeId(node)}
        position={[node.lat, node.lng]}
        icon={nodeIcon(node.number, variant, scale)}
        zIndexOffset={variant === "picked" ? 1200 : 1000}
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

function formatElapsed(ms) {
  const secs = Math.max(0, Math.round((Number(ms) || 0) / 1000));
  if (secs < 60) return `${Math.round(secs)} sec`;
  return formatDuration(Math.round(secs / 60));
}

function KnoopProgressCard({ label, item, current = false }) {
  return (
    <div className={`knoop-progress-card${current ? " current" : ""}`}>
      <span className="kicker">{label}</span>
      {item ? (
        <>
          <strong>{item.number}</strong>
          <small>{formatKm(item.alongM / 1000)} · {formatElapsed(item.timeMs)}</small>
        </>
      ) : (
        <strong className="knoop-progress-empty">—</strong>
      )}
    </div>
  );
}

function FitRoute({ geometry, nodes }) {
  const map = useMap();
  useEffect(() => {
    const points = [];
    for (const point of geometry || []) {
      if (Array.isArray(point) && point.length >= 2) points.push([point[0], point[1]]);
    }
    if (points.length < 2) {
      for (const node of nodes || []) {
        if (Number.isFinite(node?.lat) && Number.isFinite(node?.lng)) {
          points.push([node.lat, node.lng]);
        }
      }
    }
    if (points.length < 2) return;
    map.fitBounds(points, { padding: [48, 48], maxZoom: 14 });
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
