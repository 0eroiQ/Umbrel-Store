(() => {
  "use strict";

  const state = {
    authenticated: false,
    authenticating: false,
    observerTimer: null,
    modal: null,
    player: null,
    currentMedia: null,
    currentEpisode: null,
    progressTimer: null,
  };

  const nativeFetch = window.fetch.bind(window);
  const nativeXHROpen = XMLHttpRequest.prototype.open;
  const nativeXHRSetHeader = XMLHttpRequest.prototype.setRequestHeader;

  function tokenFromHeaders(headers) {
    if (!headers) return "";
    try {
      const normalized = new Headers(headers);
      return normalized.get("X-Plex-Token") || normalized.get("x-plex-token") || "";
    } catch (_) {
      return "";
    }
  }

  function tokenFromURL(value) {
    try {
      const raw = value instanceof Request ? value.url : String(value || "");
      return new URL(raw, window.location.href).searchParams.get("X-Plex-Token") || "";
    } catch (_) {
      return "";
    }
  }

  async function establishOwnerSession(token) {
    if (!token || state.authenticated || state.authenticating) return;
    state.authenticating = true;
    try {
      const response = await nativeFetch("/vortexo/api/session", {
        method: "PUT",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({plex_token: token}),
      });
      state.authenticated = response.ok;
      if (state.authenticated) scheduleInjection();
    } catch (_) {
      state.authenticated = false;
    } finally {
      state.authenticating = false;
    }
  }

  // Observe Plex's own authenticated requests without persisting, logging, or
  // exposing the token. The server verifies that it belongs to the same account
  // as the local Plex owner, then issues an HTTP-only session.
  window.fetch = function(input, init = {}) {
    const token = tokenFromHeaders(init.headers)
      || (input instanceof Request ? tokenFromHeaders(input.headers) : "")
      || tokenFromURL(input);
    if (token) establishOwnerSession(token);
    return nativeFetch(input, init);
  };
  XMLHttpRequest.prototype.open = function(...args) {
    const token = tokenFromURL(args[1]);
    if (token) establishOwnerSession(token);
    return nativeXHROpen.apply(this, args);
  };
  XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
    if (String(name).toLowerCase() === "x-plex-token" && value) {
      establishOwnerSession(String(value));
    }
    return nativeXHRSetHeader.call(this, name, value);
  };

  function api(path, options = {}) {
    return nativeFetch(path, {
      credentials: "same-origin",
      ...options,
      headers: {
        ...(options.body ? {"Content-Type": "application/json"} : {}),
        ...(options.headers || {}),
      },
    }).then(async response => {
      let body = {};
      try { body = await response.json(); } catch (_) {}
      if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
      return body;
    });
  }

  function discoverIDFromLocation() {
    const hash = window.location.hash || "";
    if (!hash.includes("tv.plex.provider.discover") || !hash.includes("/details?")) return "";
    const query = hash.split("/details?", 2)[1] || "";
    const params = new URLSearchParams(query);
    const key = decodeURIComponent(params.get("key") || "");
    const match = key.match(/\/library\/metadata\/([^/?#]+)/);
    return match ? match[1] : "";
  }

  function exactTextElement(text) {
    const candidates = document.querySelectorAll("h1,h2,h3,h4,[role='heading'],div,span");
    for (const candidate of candidates) {
      if (candidate.children.length > 3) continue;
      if ((candidate.textContent || "").trim() === text) return candidate;
    }
    return null;
  }

  function providerRowForHeading(heading) {
    let sibling = heading.nextElementSibling;
    while (sibling) {
      if (sibling.children && sibling.children.length) return sibling;
      sibling = sibling.nextElementSibling;
    }
    const parent = heading.parentElement;
    if (!parent) return null;
    const candidates = Array.from(parent.children).filter(
      child => child !== heading && child.children && child.children.length
    );
    return candidates[0] || null;
  }

  function createTorBoxCard(discoverID) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "vortexo-torbox-card";
    card.dataset.vortexoTorbox = discoverID;
    card.setAttribute("aria-label", "Watch with TorBox");
    card.innerHTML = `
      <span class="vortexo-torbox-mark" aria-hidden="true">
        <svg viewBox="0 0 48 48"><path d="M7 12.5 24 4l17 8.5v23L24 44 7 35.5v-23Zm8.5 5.3v7.1l5 2.5v8.3l3.5 1.8 3.5-1.8v-8.3l5-2.5v-7.1L24 22l-8.5-4.2Z"/></svg>
      </span>
      <span class="vortexo-torbox-copy">
        <strong>TorBox</strong>
        <small>Available streams</small>
      </span>`;
    card.addEventListener("click", () => openTorBox(discoverID));
    return card;
  }

  function injectCard() {
    const discoverID = discoverIDFromLocation();
    document.querySelectorAll("[data-vortexo-torbox]").forEach(card => {
      if (!state.authenticated || !discoverID || card.dataset.vortexoTorbox !== discoverID) {
        card.remove();
      }
    });
    if (
      !state.authenticated
      || !discoverID
      || document.querySelector(`[data-vortexo-torbox="${CSS.escape(discoverID)}"]`)
    ) return;
    const heading = exactTextElement("Watch from these locations");
    if (!heading) return;
    const row = providerRowForHeading(heading);
    if (!row) return;
    const card = createTorBoxCard(discoverID);
    const moreCard = Array.from(row.children).find(child => /\+\s*\d+\s*more/i.test(child.textContent || ""));
    row.insertBefore(card, moreCard || row.firstChild);
  }

  function scheduleInjection() {
    window.clearTimeout(state.observerTimer);
    state.observerTimer = window.setTimeout(injectCard, 80);
  }

  const observer = new MutationObserver(scheduleInjection);
  observer.observe(document.documentElement, {childList: true, subtree: true});
  window.addEventListener("hashchange", scheduleInjection);
  scheduleInjection();

  function modalShell(title) {
    closeModal();
    const overlay = document.createElement("div");
    overlay.className = "vortexo-overlay";
    overlay.innerHTML = `
      <section class="vortexo-modal" role="dialog" aria-modal="true" aria-label="${escapeHTML(title)}">
        <header>
          <div>
            <span class="vortexo-eyebrow">PLEX VORTEXO</span>
            <h2>${escapeHTML(title)}</h2>
          </div>
          <button type="button" class="vortexo-icon-button" data-vortexo-close aria-label="Close">×</button>
        </header>
        <div class="vortexo-modal-body"></div>
      </section>`;
    overlay.querySelector("[data-vortexo-close]").addEventListener("click", closeModal);
    overlay.addEventListener("mousedown", event => {
      if (event.target === overlay) closeModal();
    });
    document.body.appendChild(overlay);
    state.modal = overlay;
    return overlay.querySelector(".vortexo-modal-body");
  }

  function closeModal() {
    if (state.modal) state.modal.remove();
    state.modal = null;
  }

  function closePlayer() {
    if (state.progressTimer) window.clearInterval(state.progressTimer);
    state.progressTimer = null;
    if (state.player) {
      const video = state.player.querySelector("video");
      if (video) {
        try { video.pause(); } catch (_) {}
        if (video._vortexoHls) video._vortexoHls.destroy();
      }
      state.player.remove();
    }
    state.player = null;
  }

  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    if (state.player) closePlayer();
    else if (state.modal) closeModal();
  });
  window.addEventListener("popstate", () => {
    if (state.player) closePlayer();
    if (state.modal) closeModal();
  });

  async function openTorBox(discoverID) {
    const body = modalShell("TorBox");
    body.innerHTML = loading("Reading Plex Discover metadata…");
    try {
      if (!state.authenticated) {
        await waitForOwnerSession();
      }
      const status = await api("/vortexo/api/status");
      if (!status.configured) {
        renderSettings(body, status);
        return;
      }
      const media = await api(`/vortexo/api/discover/${encodeURIComponent(discoverID)}`);
      state.currentMedia = media;
      if (media.type === "show") {
        await renderEpisodePicker(body, media);
      } else {
        await searchAndRender(body, media, media.season || 0, media.episode || 0);
      }
    } catch (error) {
      renderError(body, error.message, "Open Plex Web as the server owner, then try TorBox again.");
    }
  }

  async function waitForOwnerSession() {
    const deadline = Date.now() + 3500;
    while (!state.authenticated && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (!state.authenticated) throw new Error("Plex owner session is not ready");
  }

  function loading(label) {
    return `<div class="vortexo-loading"><span></span><p>${escapeHTML(label)}</p></div>`;
  }

  function renderError(container, message, help = "") {
    container.innerHTML = `
      <div class="vortexo-empty vortexo-error">
        <strong>${escapeHTML(message)}</strong>
        ${help ? `<p>${escapeHTML(help)}</p>` : ""}
      </div>`;
  }

  function renderSettings(container, status = {}) {
    const watchlist = status.watchlist || {};
    const sync = watchlist.sync || {};
    const pollMinutes = Number(status.plex_watchlist_poll_minutes || watchlist.poll_minutes || 1);
    const profile = status.plex_watchlist_profile || watchlist.profile || "best";
    const enabled = Boolean(status.plex_watchlist_enabled ?? watchlist.enabled ?? false);
    const cachedOnly = Boolean(status.plex_watchlist_cached_only ?? watchlist.cached_only ?? true);
    const autoUpgrade = Boolean(status.plex_watchlist_auto_upgrade ?? watchlist.auto_upgrade ?? true);
    container.innerHTML = `
      <div class="vortexo-setup">
        <div class="vortexo-callout">
          <strong>Connect TorBox to Plex</strong>
          <p>The key and source URL stay on this Umbrel. They are never stored in this browser.</p>
        </div>
        <label>TorBox API key
          <input type="password" autocomplete="off" data-vortexo-key placeholder="${status.torbox_configured ? "Saved — leave blank to keep it" : "Paste API key"}">
        </label>
        <label>Vortexo Sources manifest URL
          <textarea rows="3" data-vortexo-manifest placeholder="https://…/manifest.json"></textarea>
          <small>Use an AIOStreams or Torrentio-compatible manifest that exposes the stream resource.</small>
        </label>
        <label>TorBox WebDAV URL
          <input type="url" data-vortexo-webdav value="https://webdav.torbox.app">
        </label>
        <div class="vortexo-callout">
          <strong>Use TorBox from every Plex client</strong>
          <p>Add a title to the normal Plex Watchlist. Vortexo selects a cached release, adds it to the Plex library, and Plex clients see it as regular media. TV shows safely start with the first regular episode.</p>
        </div>
        <label class="vortexo-check">
          <input type="checkbox" data-vortexo-watchlist-enabled ${enabled ? "checked" : ""}>
          <span>Automatically import my Plex Watchlist</span>
        </label>
        <div class="vortexo-settings-grid">
          <label>Check Watchlist
            <select data-vortexo-watchlist-interval>
              ${[1, 5, 15, 30, 60].map(value => `<option value="${value}" ${pollMinutes === value ? "selected" : ""}>Every ${value} minute${value === 1 ? "" : "s"}</option>`).join("")}
            </select>
          </label>
          <label>Automatic quality
            <select data-vortexo-watchlist-profile>
              <option value="best" ${profile === "best" ? "selected" : ""}>Best available</option>
              <option value="4k" ${profile === "4k" ? "selected" : ""}>Prefer 4K</option>
              <option value="1080p" ${profile === "1080p" ? "selected" : ""}>Prefer 1080p</option>
            </select>
          </label>
          <label>Maximum release size (GB)
            <input type="number" min="0" max="1000" step="1" data-vortexo-watchlist-size value="${Number(status.plex_watchlist_max_size_gb ?? 80)}">
          </label>
          <label>Maximum Watchlist items
            <input type="number" min="1" max="1000" step="1" data-vortexo-watchlist-limit value="${Number(status.plex_watchlist_max_items ?? 100)}">
          </label>
        </div>
        <label class="vortexo-check">
          <input type="checkbox" data-vortexo-watchlist-cached ${cachedOnly ? "checked" : ""}>
          <span>Cached TorBox releases only</span>
        </label>
        <label class="vortexo-check">
          <input type="checkbox" data-vortexo-watchlist-upgrade ${autoUpgrade ? "checked" : ""}>
          <span>Automatically add a safer higher-quality version when one becomes available</span>
        </label>
        <div class="vortexo-actions">
          <button class="vortexo-secondary" data-vortexo-sync>Sync Watchlist now</button>
          <button class="vortexo-primary" data-vortexo-save>Save and connect</button>
        </div>
        <p class="vortexo-form-status" role="status">${escapeHTML(sync.detail || "")}</p>
      </div>`;
    const save = container.querySelector("[data-vortexo-save]");
    const syncNow = container.querySelector("[data-vortexo-sync]");
    syncNow.addEventListener("click", async () => {
      const statusLine = container.querySelector(".vortexo-form-status");
      syncNow.disabled = true;
      statusLine.textContent = "Reading Plex Watchlist…";
      try {
        const result = await api("/vortexo/api/watchlist/sync", {
          method: "POST",
          body: "{}",
        });
        statusLine.textContent = result.detail || "Watchlist sync completed";
      } catch (error) {
        statusLine.textContent = error.message;
      } finally {
        syncNow.disabled = false;
      }
    });
    save.addEventListener("click", async () => {
      const statusLine = container.querySelector(".vortexo-form-status");
      const key = container.querySelector("[data-vortexo-key]").value.trim();
      const manifests = container.querySelector("[data-vortexo-manifest]").value
        .split("\n").map(value => value.trim()).filter(Boolean);
      save.disabled = true;
      statusLine.textContent = "Validating TorBox…";
      try {
        await api("/vortexo/api/settings", {
          method: "PUT",
          body: JSON.stringify({
            ...(key ? {torbox_api_key: key} : {}),
            stream_manifest_urls: manifests,
            webdav_url: container.querySelector("[data-vortexo-webdav]").value.trim(),
            plex_watchlist_enabled: container.querySelector("[data-vortexo-watchlist-enabled]").checked,
            plex_watchlist_poll_minutes: Number(container.querySelector("[data-vortexo-watchlist-interval]").value),
            plex_watchlist_profile: container.querySelector("[data-vortexo-watchlist-profile]").value,
            plex_watchlist_max_size_gb: Number(container.querySelector("[data-vortexo-watchlist-size]").value),
            plex_watchlist_max_items: Number(container.querySelector("[data-vortexo-watchlist-limit]").value),
            plex_watchlist_cached_only: container.querySelector("[data-vortexo-watchlist-cached]").checked,
            plex_watchlist_auto_upgrade: container.querySelector("[data-vortexo-watchlist-upgrade]").checked,
            plex_watchlist_show_mode: "first_episode",
          }),
        });
        statusLine.textContent = "Connected. Opening streams…";
        await openTorBox(discoverIDFromLocation());
      } catch (error) {
        statusLine.textContent = error.message;
      } finally {
        save.disabled = false;
      }
    });
  }

  async function renderEpisodePicker(container, media) {
    container.innerHTML = loading("Loading seasons and episodes…");
    try {
      const response = await api(`/vortexo/api/discover/${encodeURIComponent(media.discover_id)}/episodes`);
      const episodes = response.episodes || [];
      if (!episodes.length) {
        renderError(container, "No episodes were returned by Plex Discover");
        return;
      }
      const seasons = [...new Set(episodes.map(item => Number(item.season || 0)))].filter(Boolean);
      container.innerHTML = `
        <div class="vortexo-media-heading">
          <div><span>TV SERIES</span><h3>${escapeHTML(media.title)}</h3></div>
        </div>
        <div class="vortexo-picker">
          <label>Season<select data-vortexo-season></select></label>
          <label>Episode<select data-vortexo-episode></select></label>
          <button class="vortexo-primary" data-vortexo-find>Find streams</button>
        </div>`;
      const seasonSelect = container.querySelector("[data-vortexo-season]");
      const episodeSelect = container.querySelector("[data-vortexo-episode]");
      seasons.forEach(season => {
        seasonSelect.add(new Option(`Season ${season}`, String(season)));
      });
      const populateEpisodes = () => {
        const selectedSeason = Number(seasonSelect.value);
        episodeSelect.replaceChildren();
        episodes.filter(item => Number(item.season) === selectedSeason).forEach(item => {
          const option = new Option(
            `E${String(item.episode).padStart(2, "0")} · ${item.title}`,
            String(item.episode)
          );
          option._vortexoMedia = item;
          episodeSelect.add(option);
        });
      };
      seasonSelect.addEventListener("change", populateEpisodes);
      populateEpisodes();
      container.querySelector("[data-vortexo-find]").addEventListener("click", async () => {
        const selected = episodeSelect.options[episodeSelect.selectedIndex];
        state.currentEpisode = selected?._vortexoMedia || null;
        await searchAndRender(
          container,
          state.currentEpisode || media,
          Number(seasonSelect.value),
          Number(episodeSelect.value)
        );
      });
    } catch (error) {
      renderError(container, error.message);
    }
  }

  async function searchAndRender(container, media, season, episode) {
    container.innerHTML = loading("Searching Vortexo Sources and checking TorBox…");
    try {
      const response = await api("/vortexo/api/streams", {
        method: "POST",
        body: JSON.stringify({
          discover_id: state.currentMedia?.discover_id || media.discover_id,
          ...(media.type === "episode" ? {episode_discover_id: media.discover_id} : {}),
          season,
          episode,
        }),
      });
      const resultMedia = response.media || media;
      resultMedia.playback_discover_id = response.playback_discover_id || resultMedia.discover_id;
      renderStreams(container, resultMedia, response.streams || [], season, episode, response.warnings || []);
    } catch (error) {
      renderError(container, error.message);
    }
  }

  function renderStreams(container, media, streams, season, episode, warnings) {
    const subtitle = season && episode
      ? `Season ${season} · Episode ${episode}`
      : [media.year, media.type === "movie" ? "Movie" : "Episode"].filter(Boolean).join(" · ");
    container.innerHTML = `
      <div class="vortexo-media-heading">
        <div><span>${escapeHTML(subtitle)}</span><h3>${escapeHTML(media.parent_title || media.title)}</h3></div>
        <button class="vortexo-quiet" data-vortexo-settings>Settings</button>
      </div>
      ${streams.length ? `<div class="vortexo-stream-list"></div>` : `
        <div class="vortexo-empty"><strong>No TorBox streams found</strong><p>Try another source manifest or episode.</p></div>`}
      ${warnings.length ? `<details class="vortexo-warnings"><summary>Source warnings</summary><p>${warnings.map(escapeHTML).join("<br>")}</p></details>` : ""}`;
    container.querySelector("[data-vortexo-settings]").addEventListener("click", async () => {
      const settings = await api("/vortexo/api/settings");
      renderSettings(container, settings);
      const textarea = container.querySelector("[data-vortexo-manifest]");
      if (textarea) textarea.value = (settings.stream_manifest_urls || []).join("\n");
      const webdav = container.querySelector("[data-vortexo-webdav]");
      if (webdav) webdav.value = settings.webdav_url || "https://webdav.torbox.app";
    });
    const list = container.querySelector(".vortexo-stream-list");
    if (!list) return;
    streams.forEach(stream => {
      const row = document.createElement("article");
      row.className = "vortexo-stream";
      row.innerHTML = `
        <div class="vortexo-stream-main">
          <div class="vortexo-badges">
            ${stream.cached ? '<span class="is-cached">CACHED</span>' : '<span>TORBOX</span>'}
            ${stream.quality ? `<span>${escapeHTML(stream.quality)}</span>` : ""}
            ${stream.dynamic_range ? `<span>${escapeHTML(stream.dynamic_range)}</span>` : ""}
          </div>
          <strong>${escapeHTML(stream.file_name || stream.label || "TorBox stream")}</strong>
          <small>${escapeHTML([
            stream.codec, stream.audio, stream.size_gb ? `${stream.size_gb} GB` : "",
            stream.seeders ? `${stream.seeders} seeders` : "", stream.source
          ].filter(Boolean).join(" · "))}</small>
        </div>
        <div class="vortexo-stream-actions">
          <button class="vortexo-primary" data-play ${stream.can_play_now ? "" : "disabled"}>Play Now</button>
          <button class="vortexo-secondary" data-add ${stream.can_add ? "" : "disabled"}>Add to Plex</button>
        </div>
        <div class="vortexo-job-status" role="status"></div>`;
      row.querySelector("[data-play]").addEventListener("click", () => playStream(stream, media, season, episode));
      row.querySelector("[data-add]").addEventListener("click", () => addToPlex(row, stream, media, season, episode));
      list.appendChild(row);
    });
  }

  async function loadHlsLibrary() {
    if (window.Hls) return window.Hls;
    await new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = "/vortexo/assets/hls.min.js";
      script.onload = resolve;
      script.onerror = reject;
      document.head.appendChild(script);
    });
    return window.Hls;
  }

  async function playStream(stream, media, season, episode) {
    try {
      const response = await api("/vortexo/api/play", {
        method: "POST",
        body: JSON.stringify({
          stream_id: stream.id,
          discover_id: media.playback_discover_id || media.discover_id,
          season,
          episode,
        }),
      });
      closePlayer();
      const overlay = document.createElement("div");
      overlay.className = "vortexo-player-overlay";
      overlay.innerHTML = `
        <div class="vortexo-player-top">
          <div><span>PLAYING FROM TORBOX</span><strong>${escapeHTML(stream.file_name || media.title)}</strong></div>
          <button class="vortexo-icon-button" data-player-close aria-label="Close player">×</button>
        </div>
        <video controls autoplay playsinline></video>`;
      overlay.querySelector("[data-player-close]").addEventListener("click", closePlayer);
      document.body.appendChild(overlay);
      state.player = overlay;
      closeModal();
      const video = overlay.querySelector("video");
      if (response.mode === "hls" && !video.canPlayType("application/vnd.apple.mpegurl")) {
        const Hls = await loadHlsLibrary();
        if (!Hls.isSupported()) throw new Error("This browser cannot play the prepared HLS stream");
        const hls = new Hls({enableWorker: true, lowLatencyMode: false});
        video._vortexoHls = hls;
        hls.loadSource(response.play_url);
        hls.attachMedia(video);
      } else {
        video.src = response.play_url;
      }
      video.addEventListener("error", () => {
        if (!state.player) return;
        closePlayer();
        renderError(
          modalShell("TorBox playback"),
          "The TorBox stream stopped before playback could continue."
        );
      }, {once: true});
      video.addEventListener("loadedmetadata", () => {
        const resume = Number(response.resume?.position_ms || 0) / 1000;
        if (resume > 10 && resume < video.duration - 30) video.currentTime = resume;
        video.play().catch(() => {});
      }, {once: true});
      const report = () => {
        if (!Number.isFinite(video.currentTime) || !Number.isFinite(video.duration)) return;
        api("/vortexo/api/progress", {
          method: "POST",
          body: JSON.stringify({
            discover_id: media.playback_discover_id || media.discover_id,
            position_ms: Math.round(video.currentTime * 1000),
            duration_ms: Math.round(video.duration * 1000),
            state: video.ended ? "stopped" : (video.paused ? "paused" : "playing"),
          }),
        }).catch(() => {});
      };
      state.progressTimer = window.setInterval(report, 15000);
      video.addEventListener("pause", report);
      video.addEventListener("ended", report);
    } catch (error) {
      closePlayer();
      renderError(modalShell("TorBox playback"), error.message);
    }
  }

  async function addToPlex(row, stream, media, season, episode) {
    const status = row.querySelector(".vortexo-job-status");
    const button = row.querySelector("[data-add]");
    button.disabled = true;
    status.textContent = "Sending release to TorBox…";
    try {
      const response = await api("/vortexo/api/library-jobs", {
        method: "POST",
        body: JSON.stringify({
          stream_id: stream.id,
          discover_id: media.playback_discover_id || media.discover_id,
          season,
          episode,
        }),
      });
      await pollJob(response.job.id, status);
    } catch (error) {
      status.textContent = error.message;
      status.classList.add("is-error");
      button.disabled = false;
    }
  }

  async function pollJob(jobID, status) {
    const terminal = new Set(["plex_confirmed", "already_in_plex", "failed"]);
    while (true) {
      const response = await api(`/vortexo/api/library-jobs/${jobID}`);
      const job = response.job;
      status.textContent = job.detail;
      status.dataset.state = job.status;
      if (terminal.has(job.status)) {
        if (job.status === "failed") status.classList.add("is-error");
        else status.classList.add("is-success");
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 3000));
    }
  }

  function escapeHTML(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
    })[character]);
  }
})();
