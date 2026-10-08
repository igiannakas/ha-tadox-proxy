/*
 * Tado X Proxy – room card
 *
 * One card per room for a tadox_proxy thermostat: current → target
 * temperature, heating %, what the room is doing right now, and five mode
 * buttons (Off · Night · Day · Boost · Schedule). Boost asks for confirmation
 * in a built-in dialog. No templates or card-mod needed.
 *
 *   type: custom:tadox-room-card
 *   entity: climate.living_room_thermostat
 *   name: Living Room          # optional, defaults to the area name
 *   icon: mdi:sofa             # optional, defaults to the area icon
 *   heating_entity: sensor.x   # optional, found automatically for Tado X
 *
 * Served and registered by the tadox_proxy integration; no resource to add.
 */

const CARD_VERSION = "1.4.1";

const MODE_NAMES = {
  comfort: "Day",
  eco: "Night",
  away: "Away",
  frost_protection: "Off",
  boost: "Boost",
  none: "Manual",
  schedule: "Schedule",
};

const BUTTONS = [
  { preset: "frost_protection", label: "Off", icon: "mdi:radiator-off", color: "var(--cyan-color, #00bcd4)" },
  { preset: "eco", label: "Night", icon: "mdi:weather-night", color: "var(--purple-color, #926bc7)" },
  { preset: "comfort", label: "Day", icon: "mdi:weather-sunny", color: "var(--amber-color, #ffc107)" },
  { preset: "boost", label: "Boost", icon: "mdi:fire", color: "var(--red-color, #f44336)", confirm: true },
  { preset: "schedule", label: "Schedule", icon: "mdi:calendar-sync", color: "var(--green-color, #4caf50)" },
];

const COLORS = {
  red: "var(--red-color, #f44336)",
  teal: "var(--teal-color, #009688)",
  grey: "var(--disabled-color, #9e9e9e)",
  blue: "var(--blue-color, #2196f3)",
};

const HOLD_MS = 500;

const num = (v) => (v === null || v === undefined || v === "" || isNaN(Number(v)) ? null : Number(v));
const fmt1 = (v) => (v === null ? "–" : v.toFixed(1));
const fmtG = (v) => (v === null ? "–" : String(Math.round(v * 10) / 10));
const tint = (color, pct) => `color-mix(in srgb, ${color} ${pct}%, transparent)`;
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/* ------------------------------------------------------------------------ */
/* Room state – everything the card shows, derived from hass in one place    */
/* ------------------------------------------------------------------------ */

function roomState(hass, config, ids) {
  const st = hass.states[config.entity];
  if (!st) return null;
  const a = st.attributes;
  const current = num(a.current_temperature);
  const target = num(a.temperature);
  const preset = a.preset_mode;
  const schedPreset = a.schedule_preset || null;
  const override = Boolean(a.schedule_override_active);

  const heatState = ids.heating ? hass.states[ids.heating] : undefined;
  const heatPct = heatState ? num(heatState.state) : null;
  const heating = heatPct !== null ? heatPct > 0 : a.hvac_action === "heating";

  const off = st.state === "off";
  const summer = Boolean(a.summer_mode_active);
  const windowOpen = Boolean(a.window_open_active);

  let status;
  if (summer) status = "Summer lock";
  else if (off) status = "Off";
  else if (windowOpen) status = "Window open";
  else if (a.presence_away_active) status = "Away · nobody home";
  else if (override) {
    status = `Override · ${preset === "none" ? `${fmt1(target)}°` : MODE_NAMES[preset] || preset}`;
    if (a.schedule_override_until) {
      const mins = Math.ceil((Date.parse(a.schedule_override_until) - Date.now()) / 60000);
      if (mins > 0) status += ` · ${mins} min`;
    }
  } else if (schedPreset) status = `Schedule · ${MODE_NAMES[schedPreset] || schedPreset}`;
  else status = MODE_NAMES[preset] || preset || "–";

  let color = COLORS.grey;
  if (off || summer || preset === "frost_protection") color = COLORS.grey;
  else if (heating) color = COLORS.red;
  else if (schedPreset && !override) color = COLORS.teal;

  let badge = null;
  if (windowOpen) badge = "mdi:window-open-variant";
  else if (override) badge = "mdi:hand-back-right";

  return {
    stateObj: st,
    current,
    target,
    preset,
    schedPreset,
    override,
    heating,
    heatPct,
    status,
    color,
    badge,
    scheduleActive: Boolean(schedPreset) && !override,
    boostTarget: ids.boost ? num(hass.states[ids.boost]?.state) : null,
    boostMinutes: num(a.boost_duration_min),
  };
}

/* Sibling entities: boost number on the proxy device, heating % on the Tado
 * device behind it (via the proxy's source_entity_id attribute). */
function discover(hass, config) {
  const ids = { heating: config.heating_entity || null, boost: null };
  const reg = hass.entities || {};
  const me = reg[config.entity];
  if (me?.device_id) {
    const boost = Object.values(reg).find(
      (e) => e.device_id === me.device_id && e.translation_key === "boost_target" && e.entity_id.startsWith("number."),
    );
    if (boost) ids.boost = boost.entity_id;
  }
  if (!ids.heating) {
    const source = hass.states[config.entity]?.attributes?.source_entity_id;
    const srcDev = source && reg[source]?.device_id;
    if (srcDev) {
      const heat = Object.values(reg).find(
        (e) => e.device_id === srcDev && e.translation_key === "heating" && e.entity_id.startsWith("sensor."),
      );
      if (heat) ids.heating = heat.entity_id;
    }
  }
  if (!ids.heating) {
    // The source may be the bare TRV (another integration) while Tado's room
    // device holds the heating %; fall back to the one Tado heating sensor in
    // the same area.
    const areaId = areaIdOf(hass, config.entity);
    const inArea = areaId
      ? Object.values(reg).filter(
          (e) =>
            e.platform === "tado" &&
            e.translation_key === "heating" &&
            e.entity_id.startsWith("sensor.") &&
            areaIdOf(hass, e.entity_id) === areaId,
        )
      : [];
    if (inArea.length === 1) ids.heating = inArea[0].entity_id;
  }
  return ids;
}

function areaIdOf(hass, entityId) {
  const ent = hass.entities?.[entityId];
  return ent?.area_id || hass.devices?.[ent?.device_id]?.area_id;
}

function areaOf(hass, entityId) {
  const areaId = areaIdOf(hass, entityId);
  return areaId ? hass.areas?.[areaId] : undefined;
}

/* ------------------------------------------------------------------------ */
/* Confirmation dialog (attached to <body> so no transform can clip it)      */
/* ------------------------------------------------------------------------ */

const DIALOG_CSS = `
  :host { position: fixed; inset: 0; z-index: 9999; display: flex; align-items: center; justify-content: center;
    font-family: var(--ha-font-family-body, Roboto, sans-serif); }
  .backdrop { position: absolute; inset: 0; background: rgba(0, 0, 0, 0.32);
    backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); animation: fade 160ms ease-out; }
  .dialog { position: relative; box-sizing: border-box; width: min(400px, calc(100vw - 32px));
    padding: 25px 18px 18px; border-radius: 32px; color: var(--primary-text-color);
    background: color-mix(in srgb, var(--card-background-color, #fff) 94%, transparent);
    backdrop-filter: blur(20px); -webkit-backdrop-filter: blur(20px);
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.25); animation: pop 180ms cubic-bezier(.2,.9,.3,1.2); }
  .row { display: flex; align-items: center; gap: 6px; padding-left: 8px; }
  .glyph { flex: 0 0 42px; height: 42px; display: flex; align-items: center; justify-content: center; border-radius: 50%; }
  .glyph ha-icon { --mdc-icon-size: 24px; }
  .title { font-size: 20px; line-height: 26px; font-weight: 600; }
  .body { margin-top: 20px; align-items: center; }
  .body .glyph { background: none; }
  .primary { font-size: 17px; line-height: 24px; font-weight: 600; }
  .secondary { font-size: 15px; line-height: 21px; color: var(--primary-text-color); margin-top: 2px; }
  .buttons { margin-top: 20px; display: grid; gap: 8px; }
  button { all: unset; box-sizing: border-box; display: flex; align-items: center; gap: 10px; height: 56px;
    padding-left: 10px; border-radius: 28px; cursor: pointer; font-size: 16px; font-weight: 600;
    background: rgba(var(--rgb-primary-text-color, 33, 33, 33), 0.07); color: var(--primary-text-color); }
  button .glyph { flex-basis: 36px; height: 36px; background: rgba(var(--rgb-primary-text-color, 33, 33, 33), 0.07); }
  button .glyph ha-icon { --mdc-icon-size: 22px; color: var(--secondary-text-color); }
  button.confirm { color: #fff; }
  button.confirm .glyph { background: rgba(255, 255, 255, 0.2); }
  button.confirm .glyph ha-icon { color: #fff; }
  button:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 2px; }
  button:active { transform: scale(0.98); }
  @keyframes fade { from { opacity: 0; } }
  @keyframes pop { from { opacity: 0; transform: scale(0.94); } }
`;

function confirmDialog({ title, icon, color, bodyIcon, primary, secondary, confirmLabel }) {
  return new Promise((resolve) => {
    const host = document.createElement("tadox-confirm-dialog");
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>${DIALOG_CSS}</style>
      <div class="backdrop"></div>
      <div class="dialog" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="row header">
          <div class="glyph" style="background:${tint(color, 20)}"><ha-icon icon="${esc(icon)}" style="color:${color}"></ha-icon></div>
          <div class="title">${esc(title)}</div>
        </div>
        <div class="row body">
          <div class="glyph"><ha-icon icon="${esc(bodyIcon)}" style="color:${color}"></ha-icon></div>
          <div><div class="primary">${esc(primary)}</div><div class="secondary">${esc(secondary)}</div></div>
        </div>
        <div class="buttons">
          <button class="confirm" style="background:${color}"><span class="glyph"><ha-icon icon="${esc(icon)}"></ha-icon></span>${esc(confirmLabel)}</button>
          <button class="cancel"><span class="glyph"><ha-icon icon="mdi:close"></ha-icon></span>Cancel</button>
        </div>
      </div>`;
    const done = (ok) => {
      document.removeEventListener("keydown", onKey, true);
      host.remove();
      resolve(ok);
    };
    const onKey = (ev) => {
      if (ev.key === "Escape") {
        ev.stopPropagation();
        done(false);
      }
    };
    root.querySelector(".backdrop").addEventListener("click", () => done(false));
    root.querySelector(".cancel").addEventListener("click", () => done(false));
    root.querySelector(".confirm").addEventListener("click", () => done(true));
    document.addEventListener("keydown", onKey, true);
    document.body.appendChild(host);
    root.querySelector(".confirm").focus();
  });
}

/* ------------------------------------------------------------------------ */
/* The card                                                                  */
/* ------------------------------------------------------------------------ */

const CARD_CSS = `
  :host { display: block; }
  ha-card { container-type: inline-size; height: 100%; overflow: hidden; }
  .head { display: flex; align-items: center; gap: 12px; padding: 12px 12px 8px; cursor: pointer;
    -webkit-tap-highlight-color: transparent; user-select: none; -webkit-user-select: none; }
  .shape { position: relative; flex: 0 0 40px; height: 40px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center; transition: background-color 180ms; }
  .shape > .icon { --mdc-icon-size: 22px; transition: color 180ms; }
  .badge { position: absolute; top: -4px; right: -4px; width: 18px; height: 18px; border-radius: 50%;
    background: var(--red-color, #f44336); display: none; align-items: center; justify-content: center; }
  .badge.on { display: flex; }
  .badge ha-icon { --mdc-icon-size: 12px; color: #fff; }
  .info { min-width: 0; flex: 1; }
  .name { font-size: 14px; line-height: 20px; font-weight: 500; color: var(--primary-text-color);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .line { font-size: 12px; line-height: 16px; color: var(--secondary-text-color); letter-spacing: .2px; }
  .temps { display: flex; align-items: center; column-gap: 3px; white-space: nowrap; overflow: hidden; }
  .temps ha-icon { --mdc-icon-size: 13px; margin-bottom: 1px; }
  .status { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .modes { display: flex; justify-content: space-between; gap: 6px; padding: 0 12px 12px; }
  .mode { all: unset; box-sizing: border-box; flex: 0 0 auto; display: flex; align-items: center; justify-content: center;
    width: 34px; height: 34px; border-radius: 17px; cursor: pointer; transition: background-color 180ms;
    background: rgba(var(--rgb-primary-text-color, 33, 33, 33), 0.05); -webkit-tap-highlight-color: transparent; }
  .mode ha-icon { --mdc-icon-size: 20px; color: var(--disabled-color, #9e9e9e); transition: color 180ms; }
  .mode:focus-visible { outline: 2px solid var(--primary-color); outline-offset: 1px; }
  .mode:active { transform: scale(0.94); }
  .mode.busy { opacity: 0.5; pointer-events: none; }
  @container (max-width: 230px) {
    .head { padding: 10px 10px 6px; gap: 8px; }
    .shape { flex-basis: 36px; height: 36px; }
    .shape > .icon { --mdc-icon-size: 20px; }
    .modes { padding: 0 10px 8px; gap: 2px; }
    .mode { width: 28px; height: 28px; }
    .mode ha-icon { --mdc-icon-size: 18px; }
  }
  @container (min-width: 190px) and (max-width: 230px) {
    .modes { padding: 0 12px 8px; }
    .mode { width: 30px; height: 30px; }
    .mode ha-icon { --mdc-icon-size: 20px; }
  }
  @container (max-width: 167px) {
    .modes { padding: 0 6px 8px; gap: 1px; }
    .mode { width: 26px; height: 26px; }
    .mode ha-icon { --mdc-icon-size: 16px; }
  }
  .warning { padding: 12px; color: var(--error-color, #db4437); }
`;

class TadoxRoomCard extends HTMLElement {
  static getStubConfig(hass) {
    const first = Object.values(hass.entities || {}).find(
      (e) => e.platform === "tadox_proxy" && e.entity_id.startsWith("climate."),
    );
    return { entity: first ? first.entity_id : "" };
  }

  static getConfigForm() {
    return {
      schema: [
        { name: "entity", required: true, selector: { entity: { domain: "climate", integration: "tadox_proxy" } } },
        {
          type: "grid",
          name: "",
          schema: [
            { name: "name", selector: { text: {} } },
            { name: "icon", selector: { icon: {} } },
          ],
        },
        { name: "heating_entity", selector: { entity: { domain: "sensor" } } },
      ],
      computeLabel: (s) =>
        ({ entity: "Thermostat", name: "Name", icon: "Icon", heating_entity: "Heating % sensor (optional)" })[s.name],
      computeHelper: (s) =>
        s.name === "heating_entity" ? "Found automatically for Tado X. Set it only if the % stays empty." : undefined,
    };
  }

  setConfig(config) {
    if (!config || !config.entity || !String(config.entity).startsWith("climate.")) {
      throw new Error("Set 'entity' to a Tado X Proxy climate entity");
    }
    this._config = { ...config };
    this._ids = null;
    this._built = false;
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 3;
  }

  getGridOptions() {
    return { columns: 6, rows: "auto", min_columns: 3 };
  }

  connectedCallback() {
    // Keep the "· 12 min" countdown fresh between state changes.
    this._timer = setInterval(() => this._hass && this._render(), 30000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  _build() {
    const root = this.shadowRoot || this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>${CARD_CSS}</style>
      <ha-card>
        <div class="head" role="button" tabindex="0" aria-label="Open thermostat (long-press)">
          <div class="shape"><ha-icon class="icon"></ha-icon><span class="badge"><ha-icon></ha-icon></span></div>
          <div class="info">
            <div class="name"></div>
            <div class="line temps"></div>
            <div class="line status"></div>
          </div>
        </div>
        <div class="modes">
          ${BUTTONS.map(
            (b) =>
              `<button class="mode" data-preset="${b.preset}" title="${b.label}" aria-label="${b.label}"><ha-icon icon="${b.icon}"></ha-icon></button>`,
          ).join("")}
        </div>
      </ha-card>`;
    this._el = {
      head: root.querySelector(".head"),
      shape: root.querySelector(".shape"),
      icon: root.querySelector(".shape .icon"),
      badge: root.querySelector(".badge"),
      badgeIcon: root.querySelector(".badge ha-icon"),
      name: root.querySelector(".name"),
      temps: root.querySelector(".temps"),
      status: root.querySelector(".status"),
      modes: [...root.querySelectorAll(".mode")],
    };
    this._wireHold(this._el.head);
    this._el.head.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") this._moreInfo();
    });
    this._el.modes.forEach((btn) => btn.addEventListener("click", () => this._onMode(btn)));
    this._built = true;
  }

  _wireHold(el) {
    let timer = null;
    let start = null;
    const cancel = () => {
      clearTimeout(timer);
      timer = null;
    };
    el.addEventListener("pointerdown", (ev) => {
      start = [ev.clientX, ev.clientY];
      cancel();
      timer = setTimeout(() => {
        timer = null;
        this._haptic("medium");
        this._moreInfo();
      }, HOLD_MS);
    });
    el.addEventListener("pointermove", (ev) => {
      if (timer && start && Math.hypot(ev.clientX - start[0], ev.clientY - start[1]) > 10) cancel();
    });
    ["pointerup", "pointercancel", "pointerleave"].forEach((t) => el.addEventListener(t, cancel));
    el.addEventListener("contextmenu", (ev) => ev.preventDefault());
  }

  _render() {
    if (!this._config || !this._hass) return;
    const hass = this._hass;
    if (!this._ids) this._ids = discover(hass, this._config);
    const s = roomState(hass, this._config, this._ids);
    if (!s) {
      const root = this.shadowRoot || this.attachShadow({ mode: "open" });
      root.innerHTML = `<style>${CARD_CSS}</style><ha-card><div class="warning">Entity not found: ${esc(this._config.entity)}</div></ha-card>`;
      this._built = false;
      return;
    }
    if (!this._built) this._build();
    const area = areaOf(hass, this._config.entity);
    const name =
      this._config.name || area?.name || (s.stateObj.attributes.friendly_name || "").replace(/\s*thermostat$/i, "");
    const icon = this._config.icon || area?.icon || "mdi:thermostat";
    this._roomName = name;
    this._roomIcon = icon;

    const e = this._el;
    e.name.textContent = name;
    e.icon.setAttribute("icon", icon);
    e.icon.style.color = s.color;
    e.shape.style.backgroundColor = tint(s.color, 20);
    e.badge.classList.toggle("on", Boolean(s.badge));
    if (s.badge) e.badgeIcon.setAttribute("icon", s.badge);

    const flame = s.heating
      ? `<ha-icon icon="mdi:fire" style="color:${COLORS.red}"></ha-icon>`
      : `<ha-icon icon="mdi:snowflake" style="color:${COLORS.blue}"></ha-icon>`;
    const pct = s.heatPct === null ? "" : `${Math.round(s.heatPct)}%`;
    const temps = `<span>${fmt1(s.current)}° → ${fmt1(s.target)}°</span>${flame}<span>${pct}</span>`;
    if (temps !== this._lastTemps) {
      e.temps.innerHTML = temps;
      this._lastTemps = temps;
    }
    e.status.textContent = s.status;

    for (const btn of e.modes) {
      const def = BUTTONS.find((b) => b.preset === btn.dataset.preset);
      const active = def.preset === "schedule" ? s.scheduleActive : s.preset === def.preset;
      btn.style.backgroundColor = active ? tint(def.color, 20) : "";
      btn.querySelector("ha-icon").style.color = active ? def.color : "";
      btn.setAttribute("aria-pressed", String(active));
    }
    this._state = s;
  }

  async _onMode(btn) {
    const def = BUTTONS.find((b) => b.preset === btn.dataset.preset);
    this._haptic("light");
    if (def.confirm) {
      const s = this._state || {};
      const parts = [];
      if (s.current !== null && s.current !== undefined) parts.push(`Currently ${fmt1(s.current)} °C.`);
      const to = s.boostTarget !== null && s.boostTarget !== undefined ? `${fmtG(s.boostTarget)} °C` : "its boost temperature";
      const forMins = s.boostMinutes ? `for ${fmtG(s.boostMinutes)} minutes` : "for the boost period";
      const after = s.schedPreset ? "then goes back to its schedule." : "then switches to Day.";
      parts.push(`Heats to ${to} ${forMins}, ${after}`);
      const ok = await confirmDialog({
        title: "Boost heating",
        icon: def.icon,
        color: def.color,
        bodyIcon: this._roomIcon,
        primary: this._roomName,
        secondary: parts.join(" "),
        confirmLabel: "Boost heating",
      });
      if (!ok) return;
    }
    btn.classList.add("busy");
    try {
      await this._hass.callService("climate", "set_preset_mode", {
        entity_id: this._config.entity,
        preset_mode: def.preset,
      });
    } catch (err) {
      this.dispatchEvent(
        new CustomEvent("hass-notification", {
          detail: { message: err?.message || `Could not set ${def.label}` },
          bubbles: true,
          composed: true,
        }),
      );
    } finally {
      btn.classList.remove("busy");
    }
  }

  _moreInfo() {
    this.dispatchEvent(
      new CustomEvent("hass-more-info", { detail: { entityId: this._config.entity }, bubbles: true, composed: true }),
    );
  }

  _haptic(kind) {
    this.dispatchEvent(new CustomEvent("haptic", { detail: kind, bubbles: true, composed: true }));
  }
}

/* Home Assistant swaps in a scoped custom-element registry while its frontend
 * boots, and this module (loaded by the integration as an extra module) can run
 * before that. An element defined in the original registry still renders, but
 * the dashboard asks the new registry and reports "custom element doesn't
 * exist". So wait until the app itself is defined, then register through
 * whichever registry is current at that point. */
async function registerCard() {
  await window.customElements.whenDefined("home-assistant");
  const registry = window.customElements;
  if (registry.get("tadox-room-card")) return;
  registry.define("tadox-room-card", TadoxRoomCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "tadox-room-card",
    name: "Tado X room",
    description: "Room temperature, heating and mode buttons for a Tado X Proxy thermostat.",
    preview: true,
    documentationURL: "https://github.com/kinimodb/ha-tadox-proxy/blob/main/docs/dashboard-card.md",
  });
  console.info(`%c TADOX-ROOM-CARD %c ${CARD_VERSION} `, "background:#f44336;color:#fff", "");
}

registerCard();
