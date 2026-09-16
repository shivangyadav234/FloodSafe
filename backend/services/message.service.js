'use strict';

/**
 * Generate the SOS SMS message body.
 *
 * @param {object} p
 * @param {string}       p.userName
 * @param {object|null}  p.location   — { lat, lng } or null
 * @param {string}       p.timestamp  — ISO 8601 string
 * @param {boolean}      p.isTest
 * @returns {string}
 */
function generateSOSMessage({ userName, location, timestamp, isTest }) {
  const prefix = isTest ? '⚠️ [TEST] ' : '';

  let locationText;
  if (location && location.lat != null && location.lng != null) {
    const mapsLink = `https://maps.google.com/?q=${location.lat},${location.lng}`;
    locationText = `My current location:\n${mapsLink}`;
  } else {
    locationText = 'Location unavailable.';
  }

  const time = new Date(timestamp).toLocaleString('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Kolkata',
  });

  return [
    `${prefix}🚨 EMERGENCY SOS ALERT`,
    '',
    `Help! I need assistance.`,
    '',
    locationText,
    '',
    `Sender: ${userName}`,
    `Time: ${time}`,
    '',
    `Track flood risk & safe routes: https://floodsafe-u207.onrender.com/`,
  ].join('\n');
}

/**
 * Generate the SMS body for an authority flood alert (FFGS -> SOS interface).
 * Distinct from generateSOSMessage: this is the model warning a district
 * authority about a ward crossing HIGH/CRITICAL risk, not a person
 * requesting personal help.
 *
 * @param {object} p
 * @param {string}       p.wardName
 * @param {string|null}  p.district
 * @param {string}       p.riskLevel        — HIGH | CRITICAL
 * @param {number}       p.floodProbability — 0-1 ward risk score
 * @param {string}       p.validFor         — ISO 8601 string
 * @returns {string}
 */
function generateAuthorityAlertMessage({ wardName, district, riskLevel, floodProbability, validFor }) {
  const pct = Math.round((floodProbability ?? 0) * 100);
  const time = new Date(validFor).toLocaleString('en-IN', {
    dateStyle: 'medium',
    timeStyle: 'short',
    timeZone: 'Asia/Kolkata',
  });
  const districtLine = district ? `District: ${district}` : null;

  return [
    `🚨 FFGS FLOOD ALERT — ${riskLevel}`,
    '',
    `Ward: ${wardName}`,
    districtLine,
    `Flood risk score: ${pct}%`,
    `Valid for: ${time}`,
    '',
    'Automated alert from the Flash Flood Guidance System. Verify and dispatch response per local protocol.',
  ]
    .filter(Boolean)
    .join('\n');
}

module.exports = { generateSOSMessage, generateAuthorityAlertMessage };
