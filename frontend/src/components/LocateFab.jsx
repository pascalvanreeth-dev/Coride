export default function LocateFab({ onClick, disabled = false, busy = false }) {
  return (
    <button
      type="button"
      className={`locate-fab ${busy ? "busy" : ""}`}
      onClick={onClick}
      disabled={disabled}
      title="Toon mijn locatie"
      aria-label="Toon mijn locatie"
    >
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="5" />
        <circle cx="12" cy="12" r="1.75" fill="currentColor" stroke="none" />
        <path d="M12 8v2M12 14v2M8 12h2M14 12h2" />
      </svg>
    </button>
  );
}
