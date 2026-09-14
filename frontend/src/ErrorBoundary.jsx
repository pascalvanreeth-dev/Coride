import React from "react";

/** Voorkomt volledig wit scherm bij een render-crash. */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("Veloverhaal crash:", error, info?.componentStack);
    try {
      sessionStorage.setItem(
        "veloverhaal_last_crash",
        JSON.stringify({
          message: String(error?.message || error),
          stack: String(error?.stack || ""),
          componentStack: String(info?.componentStack || ""),
          at: Date.now(),
        }),
      );
    } catch {
      /* ignore */
    }
  }

  render() {
    if (this.state.error) {
      const msg = String(this.state.error?.message || this.state.error);
      return (
        <div className="app-error" role="alert" style={{ padding: "2rem", fontFamily: "system-ui", maxWidth: 640 }}>
          <h1>Er ging iets mis</h1>
          <p>Herlaad de pagina of kies opnieuw Plan mijn tocht.</p>
          <pre
            style={{
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              background: "#f4f4f5",
              padding: "12px",
              borderRadius: 8,
              fontSize: 13,
            }}
          >
            {msg}
          </pre>
          <button type="button" onClick={() => window.location.reload()}>
            Pagina herladen
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
