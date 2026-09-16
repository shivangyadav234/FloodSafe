import { useEffect, useState } from "react";
import { getWardDetail, FfgsApiError, type WardDetail } from "../services/ffgsApiService";
import RainfallPanel from "./RainfallPanel";
import "./WardDetailPanel.css";

const RISK_COLORS: Record<string, string> = {
  LOW: "#22c55e",
  MODERATE: "#eab308",
  HIGH: "#f97316",
  CRITICAL: "#ef4444",
};

function confidenceLabel(confidence: number | null): string {
  if (confidence == null) return "—";
  if (confidence >= 0.8) return "HIGH";
  if (confidence >= 0.5) return "MEDIUM";
  return "LOW";
}

function threatLabel(threat: number | null): string {
  if (threat == null) return "—";
  if (threat >= 0.8) return "SEVERE";
  if (threat >= 0.6) return "HIGH";
  if (threat >= 0.4) return "MODERATE";
  return "LOW";
}

function fmt(value: number | null, digits = 1, suffix = ""): string {
  return value == null ? "—" : `${value.toFixed(digits)}${suffix}`;
}

function fmtPct(value: number | null): string {
  return value == null ? "—" : `${Math.round(value * 100)}%`;
}

interface WardDetailPanelProps {
  wardId: number;
  onClose: () => void;
}

function WardDetailPanel({ wardId, onClose }: WardDetailPanelProps) {
  const [detail, setDetail] = useState<WardDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  // A 404 here means this ward has no scored risk data yet (e.g. uninhabited
  // terrain) -- expected per the API's own docs, not a real error.
  const [noData, setNoData] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    setNoData(false);

    getWardDetail(wardId)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof FfgsApiError && err.status === 404) {
          setNoData(true);
        } else {
          setError(err.message || "Failed to load ward data.");
        }
      });

    return () => {
      cancelled = true;
    };
  }, [wardId]);

  return (
    <div className="ward-panel-overlay" onClick={onClose}>
      <div className="ward-panel" onClick={(e) => e.stopPropagation()}>
        {error && <div className="ward-panel-error">⚠ {error}</div>}

        {noData && (
          <div className="ward-panel-loading">
            No flood-risk data is available yet for this ward.
          </div>
        )}

        {!error && !noData && !detail && <div className="ward-panel-loading">Loading ward data…</div>}

        {detail && (
          <>
            <div className="ward-panel-header">
              <h2>WARD: {detail.ward_name}</h2>
              <button className="ward-panel-close" onClick={onClose} aria-label="Close">
                ✕
              </button>
            </div>

            <span
              className="ward-panel-badge"
              style={{
                background: `${RISK_COLORS[detail.risk_level ?? "LOW"]}22`,
                color: RISK_COLORS[detail.risk_level ?? "LOW"],
                border: `1px solid ${RISK_COLORS[detail.risk_level ?? "LOW"]}55`,
              }}
            >
              {detail.risk_level ?? "UNKNOWN"}
            </span>

            <div className="ward-panel-grid">
              <div className="ward-panel-stat">
                <span className="label">Flood Probability</span>
                <span className="value">{fmtPct(detail.flood_probability)}</span>
              </div>
              <div className="ward-panel-stat">
                <span className="label">Risk Level</span>
                <span className="value" style={{ color: RISK_COLORS[detail.risk_level ?? "LOW"] }}>
                  {detail.risk_level ?? "—"}
                </span>
              </div>
              <div className="ward-panel-stat">
                <span className="label">Confidence</span>
                <span className="value">{confidenceLabel(detail.confidence)}</span>
              </div>
              <div className="ward-panel-stat">
                <span className="label">Est. Lead Time</span>
                <span className="value">
                  {detail.lead_time_minutes != null ? `${detail.lead_time_minutes} min` : "N/A"}
                </span>
              </div>
            </div>
            {detail.lead_time_minutes == null && (
              <p className="ward-panel-note">
                Lead time requires forecast rainfall, which isn't ingested yet — the model only scores
                already-observed conditions.
              </p>
            )}

            <div className="ward-panel-section-title">Rainfall</div>
            {(["1h", "3h", "6h", "12h", "24h"] as const).map((w) => (
              <div className="ward-panel-rainfall-row" key={w}>
                <span>{w}</span>
                <strong>{fmt(detail.rainfall_mm[w], 1, " mm")}</strong>
              </div>
            ))}

            <div className="ward-panel-section-title">Hydrology</div>
            <div className="ward-panel-grid">
              <div className="ward-panel-stat">
                <span className="label">Soil Moisture</span>
                <span className="value">{fmt(detail.soil_moisture_pct, 0, "%")}</span>
              </div>
              <div className="ward-panel-stat">
                <span className="label">Hydrological Threat</span>
                <span className="value">{threatLabel(detail.hydrological_threat)}</span>
              </div>
              <div className="ward-panel-stat">
                <span className="label">High-Risk Area</span>
                <span className="value">{fmt(detail.high_risk_area_pct, 0, "%")}</span>
              </div>
              <div className="ward-panel-stat">
                <span className="label">Population Exposed</span>
                <span className="value">
                  {detail.population_exposed != null ? detail.population_exposed.toLocaleString("en-IN") : "—"}
                </span>
              </div>
            </div>

            <div className="ward-panel-section-title">Rainfall vs. Dynamic Threshold</div>
            <RainfallPanel wardId={wardId} />
          </>
        )}
      </div>
    </div>
  );
}

export default WardDetailPanel;
