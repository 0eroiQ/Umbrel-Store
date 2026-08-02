const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options,
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Something went wrong");
  return data;
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => element.classList.remove("show"), 2600);
}

function showView(name) {
  $$(".view").forEach(view => view.classList.toggle("active", view.id === `view-${name}`));
  $$(".nav").forEach(button => button.classList.toggle("active", button.dataset.view === name));
  scrollTo({top: 0, behavior: "smooth"});
  if (name === "lists") loadLists();
  if (name === "library") loadLibrary();
  if (name === "activity") loadRequests();
}

$$("[data-view]").forEach(button => {
  button.addEventListener("click", () => showView(button.dataset.view));
});

function esc(value = "") {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

function statusLabel(value) {
  return ({
    queued: "Queued",
    searching: "Finding release",
    library_pending: "Waiting for library",
    ready: "Ready in Plex",
    needs_attention: "Needs attention",
  })[value] || value.replaceAll("_", " ");
}

function requestRows(items) {
  if (!items.length) return '<div class="empty-state">Nothing requested yet.</div>';
  return items.map(request => `
    <article class="request-row">
      <div class="request-badge">${request.media_type === "show" ? "TV" : "▶"}</div>
      <div>
        <h3>${esc(request.title)} ${request.year ? `<span class="muted">(${request.year})</span>` : ""}</h3>
        <p>${esc(request.source)} · ${esc(request.status_detail || "Orbit is preparing this request")}</p>
      </div>
      <span class="status status-${request.status}">${esc(statusLabel(request.status))}</span>
    </article>
  `).join("");
}

function mountError(mount = {}) {
  return mount.message || mount.last_error || mount.storage_safety_error || mount.error || "Mount needs attention";
}

function showMountStatus(mount = {}) {
  const mounted = !!mount.mounted;
  const message = mounted ? "Mounted" : mountError(mount);
  $("#mount-state").textContent = mounted ? "Mounted" : "Offline";
  const pill = $("#system-pill");
  pill.className = `system-pill ${mounted ? "good" : "bad"}`;
  pill.querySelector("span").textContent = mounted ? "System operational" : message;
  const detail = $("#mount-message");
  if (detail) detail.textContent = mounted ? "TorBox mount is online" : message;
}

async function loadDashboard() {
  try {
    const dashboard = await api("/api/dashboard");
    const counts = dashboard.requests || {};
    $("#count-library").textContent = dashboard.plex_library?.item_count || 0;
    $("#count-ready").textContent = counts.ready || 0;
    $("#count-active").textContent = (counts.queued || 0) + (counts.searching || 0) + (counts.library_pending || 0);
    $("#count-lists").textContent = dashboard.active_lists || 0;
    showMountStatus(dashboard.mount);
    const requests = await api("/api/requests");
    $("#recent-requests").className = "request-list";
    $("#recent-requests").innerHTML = requestRows(requests.requests.slice(0, 5));
  } catch (error) {
    $("#system-pill").className = "system-pill bad";
    $("#system-pill span").textContent = error.message || "Orbit needs attention";
  }
}

$("#search-form").addEventListener("submit", async event => {
  event.preventDefault();
  const query = $("#search-input").value.trim();
  if (!query) return;
  const message = $("#search-message");
  const grid = $("#search-results");
  message.textContent = "Searching the universe…";
  grid.innerHTML = "";
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);
    message.textContent = `${data.results.length} result${data.results.length === 1 ? "" : "s"}`;
    grid.innerHTML = data.results.map((result, index) => {
      const owned = result.plex;
      const action = owned
        ? (owned.upgrade_available
          ? `<button class="primary poster-action upgrade" data-upgrade="${index}" aria-label="Upgrade ${esc(result.title)}">Upgrade</button>`
          : `<span class="owned-mark" aria-label="${esc(result.title)} is already in Plex">✓</span>`)
        : `<button class="primary poster-action" data-add="${index}" aria-label="Add ${esc(result.title)}">+</button>`;
      const quality = owned ? `<span class="quality-chip">In Plex · ${esc(owned.quality)}</span>` : "";
      return `<article class="poster-card">
        ${result.poster_path ? `<img loading="lazy" src="https://image.tmdb.org/t/p/w342${result.poster_path}" alt="">` : '<div class="poster-placeholder">✦</div>'}
        ${quality}
        <div class="info"><h3>${esc(result.title)}</h3><p>${result.media_type === "show" ? "TV series" : "Movie"} · ${result.year || "Year unknown"}</p></div>
        ${action}
      </article>`;
    }).join("");
    grid.querySelectorAll("[data-add]").forEach(button => {
      button.addEventListener("click", () => addRequest(data.results[+button.dataset.add], button, false));
    });
    grid.querySelectorAll("[data-upgrade]").forEach(button => {
      button.addEventListener("click", () => addRequest(data.results[+button.dataset.upgrade], button, true));
    });
  } catch (error) {
    message.textContent = error.message;
  }
});

async function addRequest(item, button, upgrade = false) {
  button.disabled = true;
  try {
    const data = await api("/api/requests", {
      method: "POST",
      body: JSON.stringify({...item, upgrade}),
    });
    toast(data.created ? `${item.title} ${upgrade ? "upgrade " : ""}entered Orbit` : `${item.title} is already in Orbit`);
    button.textContent = "✓";
    loadDashboard();
  } catch (error) {
    toast(error.message);
    button.disabled = false;
  }
}

function formatSize(bytes = 0) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index++;
  }
  return `${value.toFixed(index > 2 ? 1 : 0)} ${units[index]}`;
}

function formatDuration(milliseconds = 0) {
  if (!milliseconds) return "";
  return `${Math.round(milliseconds / 60000)} min`;
}

const libraryState = {
  type: "",
  quality: "",
  status: "",
  sort: "title",
  view: "grid",
  offset: 0,
  limit: 120,
  items: [],
  total: 0,
  detail: null,
};

function libraryStats(stats = {}) {
  const values = [
    ["All titles", stats.total || 0, ""],
    ["Movies", stats.movies || 0, "movie"],
    ["Series", stats.shows || 0, "show"],
    ["4K", stats.four_k || 0, "4k"],
    ["1080p", stats.full_hd || 0, "1080p"],
    ["Upgrades", stats.upgrades || 0, "upgrade"],
  ];
  return values.map(([label, count, filter]) => `
    <button class="library-stat" data-stat-filter="${filter}">
      <strong>${count.toLocaleString()}</strong><span>${label}</span>
    </button>
  `).join("");
}

function libraryCards(items) {
  if (!items.length) return '<div class="empty-state library-empty">No titles match these filters.</div>';
  return items.map(item => `
    <article class="media-card" tabindex="0" role="button" data-library-id="${item.id}" aria-label="View ${esc(item.title)} details">
      <div class="media-poster">
        <div class="poster-fallback">${item.media_type === "show" ? "TV" : "▶"}</div>
        ${item.thumb ? `<img loading="lazy" src="/api/library/${item.id}/artwork" alt="">` : ""}
        <span class="type-chip">${item.media_type === "show" ? "Series" : "Movie"}</span>
        ${item.upgrade_available ? '<span class="health-chip needs">Upgrade</span>' : '<span class="health-chip good">In Plex</span>'}
      </div>
      <div class="media-card-info">
        <div><h3>${esc(item.title)}</h3><p>${item.year || "Year unknown"} · ${item.media_type === "show" ? `${item.episode_count || 0} episodes` : `Section ${esc(item.section_id)}`}</p></div>
        <strong class="card-quality">${esc(item.quality)}</strong>
      </div>
    </article>
  `).join("");
}

function libraryParams() {
  const params = new URLSearchParams({
    limit: String(libraryState.limit),
    offset: String(libraryState.offset),
    sort: libraryState.sort,
  });
  const query = $("#library-search-input").value.trim();
  if (query) params.set("q", query);
  if (libraryState.type) params.set("type", libraryState.type);
  if (libraryState.quality) params.set("quality", libraryState.quality);
  if (libraryState.status) params.set("status", libraryState.status);
  return params;
}

function bindPosterFallbacks() {
  $$("#plex-library .media-poster img").forEach(image => image.addEventListener("error", () => image.remove()));
}

async function loadLibrary(append = false) {
  const root = $("#plex-library");
  const status = $("#library-status");
  if (!append) {
    libraryState.offset = 0;
    libraryState.items = [];
    root.className = `media-library-${libraryState.view}`;
    root.innerHTML = '<div class="empty-state library-empty">Reading Plex inventory…</div>';
  }
  try {
    const data = await api(`/api/library?${libraryParams()}`);
    libraryState.items.push(...data.items);
    libraryState.total = data.stats?.filtered || 0;
    root.className = `media-library-${libraryState.view}`;
    root.innerHTML = libraryCards(libraryState.items);
    bindPosterFallbacks();
    $("#library-stats").innerHTML = libraryStats(data.stats);
    $("#library-result-count").textContent = `Showing ${libraryState.items.length.toLocaleString()} of ${libraryState.total.toLocaleString()}`;
    const sync = data.sync || {};
    status.textContent = sync.status === "ready"
      ? `Last scanned ${new Date(sync.synced_at).toLocaleString()}`
      : (sync.last_error || "Plex library has not been scanned yet.");
    $("#library-load-more").classList.toggle("hidden", libraryState.items.length >= libraryState.total);
    loadDashboard();
  } catch (error) {
    root.innerHTML = `<div class="empty-state library-empty">${esc(error.message)}</div>`;
  }
}

function resetLibrary() {
  loadLibrary(false);
}

$("#library-search-form").addEventListener("submit", event => {
  event.preventDefault();
  resetLibrary();
});

let librarySearchTimer;
$("#library-search-input").addEventListener("input", () => {
  clearTimeout(librarySearchTimer);
  librarySearchTimer = setTimeout(resetLibrary, 300);
});

$$("[data-library-type]").forEach(button => button.addEventListener("click", () => {
  libraryState.type = button.dataset.libraryType;
  $$("[data-library-type]").forEach(item => item.classList.toggle("active", item === button));
  resetLibrary();
}));

$("#library-quality").addEventListener("change", event => {
  libraryState.quality = event.target.value;
  resetLibrary();
});

$("#library-health").addEventListener("change", event => {
  libraryState.status = event.target.value;
  resetLibrary();
});

$("#library-sort").addEventListener("change", event => {
  libraryState.sort = event.target.value;
  resetLibrary();
});

$$("[data-library-view]").forEach(button => button.addEventListener("click", () => {
  libraryState.view = button.dataset.libraryView;
  $$("[data-library-view]").forEach(item => item.classList.toggle("active", item === button));
  $("#plex-library").className = `media-library-${libraryState.view}`;
}));

$("#library-load-more").addEventListener("click", () => {
  libraryState.offset = libraryState.items.length;
  loadLibrary(true);
});

$("#library-stats").addEventListener("click", event => {
  const button = event.target.closest("[data-stat-filter]");
  if (!button) return;
  const filter = button.dataset.statFilter;
  if (filter === "movie" || filter === "show") {
    $(`[data-library-type="${filter}"]`).click();
  } else if (filter === "4k" || filter === "1080p") {
    $("#library-quality").value = filter;
    libraryState.quality = filter;
    resetLibrary();
  } else if (filter === "upgrade") {
    $("#library-health").value = "upgrade";
    libraryState.status = "upgrade";
    resetLibrary();
  } else {
    libraryState.type = "";
    libraryState.quality = "";
    libraryState.status = "";
    $$("[data-library-type]").forEach(item => item.classList.toggle("active", !item.dataset.libraryType));
    $("#library-quality").value = "";
    $("#library-health").value = "";
    resetLibrary();
  }
});

function streamRows(streams = []) {
  if (!streams.length) return '<p class="detail-muted">No individual tracks reported.</p>';
  return `<div class="stream-list">${streams.map(stream => `
    <div class="stream-row">
      <span class="stream-kind ${esc(stream.type)}">${esc(stream.type)}</span>
      <strong>${esc(stream.title || stream.codec || "Unknown track")}</strong>
      <small>${[
        stream.codec,
        stream.language,
        stream.channels ? `${stream.channels} channels` : "",
        stream.width && stream.height ? `${stream.width}×${stream.height}` : "",
        stream.selected ? "Selected" : "",
        stream.forced ? "Forced" : "",
      ].filter(Boolean).map(esc).join(" · ")}</small>
    </div>
  `).join("")}</div>`;
}

function versionRows(versions = []) {
  if (!versions.length) return '<p class="detail-muted">Plex did not report media-version details.</p>';
  return versions.map((version, index) => {
    const filename = (version.file || "").split(/[\\/]/).pop();
    return `<details class="version-row stream-version" ${versions.length === 1 ? "open" : ""}>
      <summary>
        <strong>${esc(version.resolution || "Unknown")} ${version.dynamic_range && version.dynamic_range !== "SDR" ? esc(version.dynamic_range) : ""}</strong>
        <span>${[
          version.video_codec,
          version.audio_codec,
          version.container,
          version.bitrate ? `${Math.round(version.bitrate / 1000)} Mbps` : "",
          formatSize(version.size),
        ].filter(Boolean).map(esc).join(" · ") || `Media version ${index + 1}`}</span>
      </summary>
      ${filename ? `<p class="stream-file">${esc(filename)}</p>` : ""}
      ${streamRows(version.streams)}
    </details>`;
  }).join("");
}

function replacementControls(scope, label, seasonNumber = "", episodeNumber = "") {
  return `<div class="replace-control">
    <div><strong>${esc(label)}</strong><span>The current Plex stream stays available while Orbit finds the replacement.</span></div>
    <select aria-label="Replacement quality">
      <option value="best">Best available</option>
      <option value="1080p">1080p</option>
      <option value="4k">4K</option>
    </select>
    <button class="primary" data-replace-scope="${scope}" data-season-number="${seasonNumber}" data-episode-number="${episodeNumber}">Find replacement</button>
  </div>`;
}

function detailHero(item, subtitle = "") {
  return `<div class="detail-hero">
    <div class="detail-art">${item.thumb ? `<img src="/api/library/${item.id}/artwork" alt="">` : `<span>${item.media_type === "show" ? "TV" : "▶"}</span>`}</div>
    <div>
      <p class="eyebrow">${item.media_type === "show" ? "SERIES" : "MOVIE"} · PLEX SECTION ${esc(item.section_id)}</p>
      <h2>${esc(item.title)}</h2>
      <p>${item.year || "Year unknown"}${item.media_type === "show" ? ` · ${item.episode_count || 0} episodes` : ""}${subtitle ? ` · ${esc(subtitle)}` : ""}</p>
      <div class="quality-line">${esc(item.quality)}</div>
      <span class="${item.upgrade_available ? "upgrade-flag" : "owned-flag"}">${item.upgrade_available ? "Upgrade available" : "In Plex"}</span>
    </div>
  </div>`;
}

function seasonRows(item) {
  const seasons = item.seasons || [];
  if (!seasons.length) return '<p class="detail-muted">Run a new Plex scan to collect episode details.</p>';
  return `<div class="season-stack">${seasons.map((season, index) => `
    <details class="season-panel" ${index === 0 ? "open" : ""}>
      <summary>
        <span><strong>${esc(season.title)}</strong><small>${season.episode_count} episodes · ${esc(season.quality)}</small></span>
        <span class="season-chevron">⌄</span>
      </summary>
      <div class="season-actions">${replacementControls("season", `Replace all of ${season.title}`, season.number)}</div>
      <div class="episode-grid">${(season.episodes || []).map(episode => `
        <button class="episode-card" data-season="${season.number}" data-episode="${episode.episode_number}">
          <span>S${String(season.number).padStart(2, "0")}E${String(episode.episode_number).padStart(2, "0")}</span>
          <strong>${esc(episode.title)}</strong>
          <small>${[episode.quality, formatDuration(episode.duration), episode.aired_at].filter(Boolean).map(esc).join(" · ")}</small>
        </button>
      `).join("") || '<p class="detail-muted">No episode records in this scan.</p>'}</div>
    </details>
  `).join("")}</div>`;
}

function renderLibraryDetail(item) {
  const replacement = item.media_type === "show"
    ? replacementControls("series", "Replace every aired episode in this series")
    : replacementControls("movie", "Replace this movie");
  $("#library-detail-content").innerHTML = `
    ${detailHero(item)}
    <section class="detail-section">
      <h3>Current media versions <span>${(item.versions || []).length}</span></h3>
      <div class="detail-versions">${versionRows(item.versions)}</div>
    </section>
    <section class="detail-section replacement-section"><h3>Replacement</h3>${replacement}</section>
    ${item.media_type === "show" ? `<section class="detail-section"><h3>Seasons and episodes <span>${(item.seasons || []).length}</span></h3>${seasonRows(item)}</section>` : ""}
    <section class="detail-section metadata"><h3>Identifiers</h3><p>Plex ${esc(item.plex_rating_key)}${item.tmdb_id ? ` · TMDb ${item.tmdb_id}` : ""}${item.imdb_id ? ` · IMDb ${esc(item.imdb_id)}` : ""}</p><p><a href="/api/library/${item.id}/manifest" target="_blank" rel="noopener">View Orbit media JSON ↗</a></p></section>
  `;
  const image = $("#library-detail-content img");
  if (image) image.addEventListener("error", () => image.remove());
}

function renderEpisodeDetail(item, seasonNumber, episodeNumber) {
  const season = (item.seasons || []).find(value => Number(value.number) === Number(seasonNumber));
  const episode = (season?.episodes || []).find(value => Number(value.episode_number) === Number(episodeNumber));
  if (!episode) return;
  $("#library-detail-content").innerHTML = `
    <div class="episode-detail-head">
      <button class="ghost" data-back-detail>← Back to ${esc(item.title)}</button>
      <p class="eyebrow">${esc(season.title)} · S${String(season.number).padStart(2, "0")}E${String(episode.episode_number).padStart(2, "0")}</p>
      <h2>${esc(episode.title)}</h2>
      <p>${[episode.aired_at, formatDuration(episode.duration), episode.quality].filter(Boolean).map(esc).join(" · ")}</p>
      ${episode.summary ? `<p class="episode-summary">${esc(episode.summary)}</p>` : ""}
    </div>
    <section class="detail-section">
      <h3>Episode media versions <span>${(episode.versions || []).length}</span></h3>
      <div class="detail-versions">${versionRows(episode.versions)}</div>
    </section>
    <section class="detail-section replacement-section">
      <h3>Replacement</h3>
      ${replacementControls("episode", `Replace S${String(season.number).padStart(2, "0")}E${String(episode.episode_number).padStart(2, "0")} only`, season.number, episode.episode_number)}
    </section>
    <section class="detail-section metadata"><h3>Identifiers</h3><p>Plex episode ${esc(episode.plex_rating_key)}</p></section>
  `;
}

async function openLibraryDetail(itemId) {
  const dialog = $("#library-detail");
  $("#library-detail-content").innerHTML = '<div class="detail-loading">Loading seasons, episodes, and streams…</div>';
  dialog.showModal();
  try {
    const data = await api(`/api/library/${itemId}`);
    libraryState.detail = data.item;
    renderLibraryDetail(data.item);
  } catch (error) {
    $("#library-detail-content").innerHTML = `<div class="detail-loading">${esc(error.message)}</div>`;
  }
}

$("#plex-library").addEventListener("click", event => {
  const card = event.target.closest("[data-library-id]");
  if (card) openLibraryDetail(Number(card.dataset.libraryId));
});

$("#plex-library").addEventListener("keydown", event => {
  if ((event.key === "Enter" || event.key === " ") && event.target.matches("[data-library-id]")) {
    event.preventDefault();
    event.target.click();
  }
});

$("#library-detail-content").addEventListener("click", async event => {
  const episodeButton = event.target.closest("[data-season][data-episode]");
  if (episodeButton && libraryState.detail) {
    renderEpisodeDetail(libraryState.detail, episodeButton.dataset.season, episodeButton.dataset.episode);
    return;
  }
  if (event.target.closest("[data-back-detail]") && libraryState.detail) {
    renderLibraryDetail(libraryState.detail);
    return;
  }
  const replacementButton = event.target.closest("[data-replace-scope]");
  if (!replacementButton || !libraryState.detail) return;
  const control = replacementButton.closest(".replace-control");
  const payload = {
    scope: replacementButton.dataset.replaceScope,
    profile: control.querySelector("select").value,
  };
  if (replacementButton.dataset.seasonNumber !== "") payload.season_number = Number(replacementButton.dataset.seasonNumber);
  if (replacementButton.dataset.episodeNumber !== "") payload.episode_number = Number(replacementButton.dataset.episodeNumber);
  replacementButton.disabled = true;
  replacementButton.textContent = "Queued";
  try {
    const data = await api(`/api/library/${libraryState.detail.id}/replace`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    toast(data.created ? "Replacement search entered Orbit" : "That replacement is already moving");
    loadDashboard();
  } catch (error) {
    toast(error.message);
    replacementButton.disabled = false;
    replacementButton.textContent = "Find replacement";
  }
});

$("#close-library-detail").addEventListener("click", () => $("#library-detail").close());
$("#library-detail").addEventListener("click", event => {
  if (event.target === $("#library-detail")) $("#library-detail").close();
});

$("#refresh-library").addEventListener("click", async event => {
  event.currentTarget.disabled = true;
  $("#library-status").textContent = "Refreshing Plex library, episodes, and stream tracks…";
  try {
    const data = await api("/api/library/sync", {method: "POST", body: "{}"});
    const queued = data.series_completion?.queued || 0;
    toast(`${data.items} Plex titles refreshed${queued ? ` · ${queued} series checks queued` : ""}`);
    await loadLibrary();
  } catch (error) {
    $("#library-status").textContent = error.message;
    toast(error.message);
  } finally {
    event.currentTarget.disabled = false;
  }
});

async function loadRequests() {
  try {
    const data = await api("/api/requests");
    $("#all-requests").className = "request-list";
    $("#all-requests").innerHTML = requestRows(data.requests);
  } catch (error) {
    toast(error.message);
  }
}

$("#show-list-form").addEventListener("click", () => $("#list-form").classList.remove("hidden"));
$("#cancel-list").addEventListener("click", () => $("#list-form").classList.add("hidden"));

$("#list-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = $("#connect-list");
  const body = Object.fromEntries(new FormData(form));
  body.max_items = Number(body.max_items);
  button.disabled = true;
  button.textContent = "Connecting…";
  try {
    const data = await api("/api/lists", {method: "POST", body: JSON.stringify(body)});
    form.reset();
    form.classList.add("hidden");
    toast(data.created ? "Automatic list connected" : "Automatic list already connected; settings updated");
    await loadLists();
    loadDashboard();
  } catch (error) {
    toast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = "Connect list";
  }
});

async function loadLists() {
  try {
    const data = await api("/api/lists");
    const root = $("#list-sources");
    if (!data.lists.length) {
      root.className = "cards empty-state";
      root.textContent = "No automatic lists connected yet.";
      return;
    }
    root.className = "cards";
    root.innerHTML = data.lists.map(source => `
      <article>
        <div class="source-logo">${source.kind === "mdblist" ? "MDB" : "T"}</div>
        <div class="source-info"><h3>${esc(source.name)}</h3><p>${esc(source.kind)} · ${source.last_sync_at ? `Last synced ${new Date(source.last_sync_at).toLocaleString()}` : "Not synced yet"}${source.last_error ? ` · ${esc(source.last_error)}` : ""}</p></div>
        <button class="ghost" data-sync="${source.id}">Sync now</button>
      </article>
    `).join("");
    root.querySelectorAll("[data-sync]").forEach(button => button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "Syncing…";
      try {
        const result = await api(`/api/lists/${button.dataset.sync}/sync`, {method: "POST", body: "{}"});
        toast(`${result.added} new · ${result.skipped_existing || 0} already in Plex`);
        loadLists();
        loadDashboard();
      } catch (error) {
        toast(error.message);
        button.disabled = false;
        button.textContent = "Sync now";
      }
    }));
  } catch (error) {
    toast(error.message);
  }
}

async function loadSettings() {
  try {
    const data = await api("/api/settings");
    const form = $("#settings-form");
    Object.entries(data).forEach(([key, value]) => {
      if (!form.elements[key]) return;
      if (form.elements[key].type === "checkbox") {
        form.elements[key].checked = ["1", "true", "yes", "on"].includes(String(value).toLowerCase());
      } else {
        form.elements[key].value = value;
      }
    });
    updateDebridFields();
  } catch (error) {
    toast(error.message);
  }
}

function updateDebridFields() {
  const mode = document.forms["settings-form"]?.elements["debrid_mode"]?.value
    || $("#settings-form [name='debrid_mode']")?.value || "webdav";
  $$("[data-debrid]").forEach(label => {
    label.style.display = label.dataset.debrid === mode ? "" : "none";
  });
}

const debridModeSelect = $("#settings-form [name='debrid_mode']");
if (debridModeSelect) {
  debridModeSelect.addEventListener("change", updateDebridFields);
  updateDebridFields();
}

$$("[data-settings-tab]").forEach(button => button.addEventListener("click", () => {
  $$("[data-settings-tab]").forEach(tab => tab.classList.toggle("active", tab === button));
  $$("[data-settings-panel]").forEach(panel => {
    panel.classList.toggle("active", panel.dataset.settingsPanel === button.dataset.settingsTab);
  });
}));

$("#settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const body = Object.fromEntries(new FormData(form));
  const scraperBoxes = $$('input[type="checkbox"][name^="scraper_"]');
  scraperBoxes.forEach(box => {
    body[box.name] = box.checked ? "true" : "false";
  });
  if (!scraperBoxes.some(box => box.checked)) {
    toast("Enable at least one scraper");
    return;
  }
  const message = $("#save-message");
  message.textContent = "Saving and testing connections…";
  try {
    const result = await api("/api/settings", {method: "POST", body: JSON.stringify(body)});
    showMountStatus(result.mount || {});
    if (!result.mount?.unchanged && result.mount?.ok === false) {
      throw new Error(mountError(result.mount));
    }
    message.textContent = "Connections saved";
    if (result.mount?.unchanged) {
      toast("Settings saved; debrid mount unchanged");
    } else {
      toast(result.mount?.mounted ? "Connections saved; mount online" : "Connections saved; mount starting");
    }
    loadDashboard();
  } catch (error) {
    message.textContent = error.message;
    toast(error.message);
  }
});

$("#restart-mount").addEventListener("click", async event => {
  event.currentTarget.disabled = true;
  try {
    const result = await api("/api/mount/restart", {method: "POST", body: "{}"});
    showMountStatus(result);
    if (result.ok === false) throw new Error(mountError(result));
    toast(result.message || "Mount restarted");
    setTimeout(loadDashboard, 3500);
  } catch (error) {
    showMountStatus({error: error.message});
    toast(error.message);
  } finally {
    event.currentTarget.disabled = false;
  }
});

$("#sync-plex-watchlist").addEventListener("click", async event => {
  const button = event.currentTarget;
  const message = $("#plex-watchlist-message");
  button.disabled = true;
  message.textContent = "Reading your Plex Watchlist…";
  try {
    const result = await api("/api/plex-watchlist/sync", {
      method: "POST",
      body: "{}",
    });
    message.textContent = `${result.added} added · ${result.skipped_existing} already in Plex · ${result.skipped_requested} already in Orbit`;
    toast(`Plex Watchlist synced: ${result.added} new`);
    loadDashboard();
  } catch (error) {
    message.textContent = error.message;
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#repair-plex-streams").addEventListener("click", async event => {
  const button = event.currentTarget;
  const message = $("#plex-repair-message");
  button.disabled = true;
  message.textContent = "Checking Plex paths and TorBox streams…";
  try {
    const result = await api("/api/library/repair", {
      method: "POST",
      body: "{}",
    });
    message.textContent = `${result.repaired} links repaired · ${result.queued} replacements queued · ${result.refreshed_paths || 0} folders refreshed`;
    toast(result.error || "Plex stream protection check complete");
    loadDashboard();
  } catch (error) {
    message.textContent = error.message;
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

$("#stop-plex-scan").addEventListener("click", async event => {
  const button = event.currentTarget;
  const message = $("#plex-repair-message");
  button.disabled = true;
  message.textContent = "Stopping current Plex scans…";
  try {
    const result = await api("/api/library/cancel-scan", {
      method: "POST",
      body: "{}",
    });
    message.textContent = `Stopped scans for ${result.cancelled_sections.length} Plex libraries`;
    toast("Plex library scan stopped");
  } catch (error) {
    message.textContent = error.message;
    toast(error.message);
  } finally {
    button.disabled = false;
  }
});

loadDashboard();
loadSettings();
setInterval(loadDashboard, 15000);
