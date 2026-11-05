const SCORE_WEIGHTS = {
  brand: 28,
  readability: 20,
  poetry: 20,
  distinctiveness: 18,
  registration: 14,
};

const DIMENSIONS = [
  ["brand", "品牌感"],
  ["readability", "读写与韵律"],
  ["poetry", "诗意与意象"],
  ["distinctiveness", "独特性"],
  ["registration", "注册启发分"],
];

const SORT_MODES = {
  score: { label: "综合评分", description: "综合评分从高到低；同分时按名称字符顺序排列。" },
  registration: { label: "注册启发分", description: "注册启发分从高到低；同分时按综合评分排序。" },
  distinctiveness: { label: "独特性", description: "独特性从高到低；同分时按综合评分排序。" },
  readability: { label: "读写与韵律", description: "读写与韵律从高到低；同分时按综合评分排序。" },
  poetry: { label: "诗意与意象", description: "诗意与意象从高到低；同分时按综合评分排序。" },
  name: { label: "名称", description: "按名称字符顺序升序排列，卡片分数显示综合评分。" },
};

const state = {
  candidates: [],
  characters: [],
  filteredCandidates: [],
  filteredCharacters: [],
  stats: {},
  aiConfigured: false,
  aiModel: "",
  aiProviders: [],
  aiProviderId: "",
  generating: false,
  view: "candidates",
  query: "",
  direction: "all",
  length: "all",
  minimumScore: 0,
  sort: "score",
  candidatePage: 1,
  totalCandidates: 0,
  characterPage: 1,
  totalCharacters: 0,
  characterQuery: "",
  characterTier: "all",
  initialCharacter: "",
  characterFilter: "",
  selectedCharacter: "",
  selectedCharacterTier: 0,
  candidatePageSize: 60,
  characterPageSize: 120,
  candidateRequest: 0,
  characterRequest: 0,
};

const elements = {
  candidateCount: document.querySelector("#candidateCount"),
  characterCount: document.querySelector("#characterCount"),
  highScoreCount: document.querySelector("#highScoreCount"),
  combinationSpace: document.querySelector("#combinationSpace"),
  directionSelect: document.querySelector("#directionSelect"),
  lengthSelect: document.querySelector("#lengthSelect"),
  searchInput: document.querySelector("#searchInput"),
  scoreRange: document.querySelector("#scoreRange"),
  scoreValue: document.querySelector("#scoreValue"),
  sortSelect: document.querySelector("#sortSelect"),
  resetButton: document.querySelector("#resetButton"),
  exportButton: document.querySelector("#exportButton"),
  resultsCount: document.querySelector("#resultsCount"),
  resultsTitle: document.querySelector("#resultsTitle"),
  sortStatus: document.querySelector("#sortStatus"),
  initialFilterNotice: document.querySelector("#initialFilterNotice"),
  initialFilterText: document.querySelector("#initialFilterText"),
  clearInitialFilter: document.querySelector("#clearInitialFilter"),
  featuredStrip: document.querySelector("#featuredStrip"),
  namesList: document.querySelector("#namesList"),
  emptyState: document.querySelector("#emptyState"),
  workspace: document.querySelector("#workspace"),
  candidateView: document.querySelector("#candidateView"),
  charactersView: document.querySelector("#charactersView"),
  candidatePagination: document.querySelector("#candidatePagination"),
  candidatePrev: document.querySelector("#candidatePrev"),
  candidateNext: document.querySelector("#candidateNext"),
  candidatePageLabel: document.querySelector("#candidatePageLabel"),
  characterGrid: document.querySelector("#characterGrid"),
  characterSearch: document.querySelector("#characterSearch"),
  characterTierSelect: document.querySelector("#characterTierSelect"),
  characterPagination: document.querySelector("#characterPagination"),
  characterPrev: document.querySelector("#characterPrev"),
  characterNext: document.querySelector("#characterNext"),
  characterPageLabel: document.querySelector("#characterPageLabel"),
  characterGenerator: document.querySelector("#characterGenerator"),
  selectedCharacter: document.querySelector("#selectedCharacter"),
  generationCount: document.querySelector("#generationCount"),
  generationProvider: document.querySelector("#generationProvider"),
  generateNamesButton: document.querySelector("#generateNamesButton"),
  viewCharacterCandidates: document.querySelector("#viewCharacterCandidates"),
  generatorStatus: document.querySelector("#generatorStatus"),
};

// Keep DOM order in score order, then place each card in the shortest column.
// Unlike CSS columns, later ranks cannot move to the top when details expand.
let candidateLayoutFrame = 0;
const candidateCardObserver = new ResizeObserver(scheduleCandidateLayout);
let candidateListWidth = 0;
const candidateListObserver = new ResizeObserver(([entry]) => {
  if (entry.contentRect.width !== candidateListWidth) {
    candidateListWidth = entry.contentRect.width;
    scheduleCandidateLayout();
  }
});
candidateListObserver.observe(elements.namesList);

function scheduleCandidateLayout() {
  if (candidateLayoutFrame) return;
  candidateLayoutFrame = window.requestAnimationFrame(() => {
    candidateLayoutFrame = 0;
    layoutCandidateCards();
  });
}

function layoutCandidateCards() {
  const list = elements.namesList;
  if (state.view !== "candidates" || list.clientWidth === 0) return;
  const cards = [...list.children];
  const style = window.getComputedStyle(list);
  const columns = style.gridTemplateColumns.split(" ").length;
  const gap = parseFloat(style.columnGap) || 0;
  const cardWidth = (list.clientWidth - gap * (columns - 1)) / columns;

  cards.forEach((card) => { card.style.width = `${cardWidth}px`; });
  const heights = cards.map((card) => card.getBoundingClientRect().height);
  const columnHeights = Array(columns).fill(0);
  cards.forEach((card, index) => {
    const column = columnHeights.indexOf(Math.min(...columnHeights));
    card.style.transform = `translate(${column * (cardWidth + gap)}px, ${columnHeights[column]}px)`;
    columnHeights[column] += heights[index] + gap;
  });
  list.style.height = `${Math.max(0, ...columnHeights) - (cards.length ? gap : 0)}px`;
  list.classList.add("is-masonry");
}

document.fonts.ready.then(scheduleCandidateLayout);

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function calculateScore(scores) {
  const weightedTotal = Object.entries(SCORE_WEIGHTS).reduce(
    (total, [dimension, weight]) => total + (scores[dimension] || 0) * weight,
    0,
  );
  // 保留一位小数：整体只有十几个整数分值时，排序和筛选都失去意义。
  return Math.round(weightedTotal) / 10;
}

// 分档按当前候选池的实际分位确定，而不是写死 90/80。
// 换一次评分口径，写死的阈值就会让"高分"要么空无一人，要么遍地都是。
const thresholds = { high: 90, mid: 80 };

function updateThresholds(stats) {
  thresholds.high = Number(stats.highThreshold ?? thresholds.high);
  thresholds.mid = Number(stats.midThreshold ?? thresholds.mid);
  const ranges = {
    keyHigh: `${thresholds.high} 及以上 · 优先研究`,
    keyMid: `${thresholds.mid}–${thresholds.high} · 值得保留`,
    keyLow: `低于 ${thresholds.mid} · 需要调整`,
  };
  for (const [id, text] of Object.entries(ranges)) {
    const node = document.querySelector(`#${id}`);
    if (node) node.textContent = text;
  }
  const foot = document.querySelector("#highScoreFoot");
  if (foot) foot.textContent = `${thresholds.high} 分及以上`;
}

function scoreClass(score) {
  if (score >= thresholds.high) return "score-high";
  if (score >= thresholds.mid) return "score-mid";
  return "score-low";
}

function scoreLabel(score) {
  if (score >= thresholds.high) return "优先研究";
  if (score >= thresholds.mid) return "值得保留";
  return "需要调整";
}

function enrichCandidates(candidates) {
  return candidates.map((item) => ({
    ...item,
    score: item.score ?? calculateScore(item.scores),
    reasons: item.reasons || [],
    notes: item.notes || [],
  }));
}

async function apiRequest(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败 (${response.status})`);
  return payload;
}

function candidateQuery(offset = (state.candidatePage - 1) * state.candidatePageSize) {
  const params = new URLSearchParams({
    length: state.length,
    query: state.query,
    direction: state.direction,
    minimum_score: String(state.minimumScore),
    sort: state.sort,
    initial: state.initialCharacter,
    character: state.characterFilter,
    limit: String(state.candidatePageSize),
    offset: String(offset),
  });
  return `/api/candidates?${params}`;
}

async function refreshCandidates() {
  const requestId = ++state.candidateRequest;
  if (state.view === "candidates") elements.resultsCount.textContent = "读取中…";
  try {
    const payload = await apiRequest(candidateQuery());
    if (requestId !== state.candidateRequest) return;
    state.filteredCandidates = enrichCandidates(payload.items || []);
    state.totalCandidates = Number(payload.total || 0);
    if (state.view === "candidates") renderCandidateView();
  } catch (error) {
    if (requestId !== state.candidateRequest) return;
    if (state.view === "candidates") showLoadError(error);
  }
}

async function refreshCharacters() {
  const requestId = ++state.characterRequest;
  const offset = (state.characterPage - 1) * state.characterPageSize;
  const params = new URLSearchParams({
    query: state.characterQuery,
    tier: state.characterTier,
    limit: String(state.characterPageSize),
    offset: String(offset),
  });
  try {
    const payload = await apiRequest(`/api/characters?${params}`);
    if (requestId !== state.characterRequest) return;
    state.filteredCharacters = payload.items || [];
    state.totalCharacters = Number(payload.total || 0);
    if (state.view === "characters") renderCharacterView();
  } catch (error) {
    if (requestId !== state.characterRequest) return;
    if (state.view === "characters") showLoadError(error);
  }
}

function displayScore(item) {
  const dimension = state.sort !== "score" && state.sort !== "name";
  return {
    label: dimension ? SORT_MODES[state.sort].label : "综合评分",
    value: (dimension ? Number(item.scores[state.sort]) : item.score).toFixed(1),
    maximum: dimension ? 10 : 100,
    dimension,
  };
}

function renderScore(item) {
  const score = displayScore(item);
  return `
    <div class="score-cell" aria-label="${score.label} ${score.value} / ${score.maximum}">
      <span class="score-metric-label">${score.label}</span>
      <div class="score-value">
        <strong class="score-number ${score.dimension ? "" : scoreClass(item.score)}">${score.value}</strong>
        <span class="score-scale">/ ${score.maximum}</span>
      </div>
      <span class="score-label">${score.dimension ? `综合 ${item.score.toFixed(1)} / 100 · ` : ""}${scoreLabel(item.score)}</span>
    </div>
  `;
}

function applyCandidateFilters() {
  refreshCandidates();
}

function applyCharacterFilters() {
  refreshCharacters();
}

function renderStats(stats = state.stats) {
  elements.candidateCount.textContent = Number(stats.candidateCount || 0).toLocaleString("zh-CN");
  elements.characterCount.textContent = Number(stats.characterCount || 0).toLocaleString("zh-CN");
  elements.highScoreCount.textContent = Number(stats.highScoreCount || 0).toLocaleString("zh-CN");
  elements.combinationSpace.textContent = stats.combinationSpace || "1.785e15";
  elements.combinationSpace.title = "去除三级规范字后：6500^4 = 1,785,062,500,000,000；完整字库：8105^4 = 4,315,310,765,550,625";
}

function renderFeatured() {
  const featured = state.filteredCandidates.slice(0, 3);
  elements.featuredStrip.setAttribute("aria-label", `${SORT_MODES[state.sort].label}排序的前三项`);
  elements.featuredStrip.innerHTML = featured
    .map(
      (item, index) => {
        const score = displayScore(item);
        return `
        <article class="featured-item">
          <div class="featured-rank"><span>第 ${index + 1} 位</span><span>${score.label} ${score.value} / ${score.maximum}</span></div>
          <strong class="featured-name">${escapeHtml(item.name)}</strong>
          ${score.dimension ? `<span class="featured-secondary">综合评分 ${item.score.toFixed(1)} / 100</span>` : ""}
          <span class="featured-meta">${escapeHtml(item.direction)} · ${escapeHtml(item.pitch)}</span>
        </article>
      `;
      },
    )
    .join("");
  elements.featuredStrip.hidden = featured.length === 0;
}

function renderDimensionDetails(item) {
  const details = DIMENSIONS.map(
    ([key, label]) => `
      <div class="detail-item">
        <span>${label} · ${SCORE_WEIGHTS[key]}%</span>
        <strong>${Number(item.scores[key]).toFixed(1)}/10</strong>
      </div>
    `,
  ).join("");
  // 扣分项单独列出：低分同样需要解释，否则用户只看到"为什么高"。
  const notes = item.notes.length
    ? `<p class="source-note"><strong>评分依据：</strong></p><ul class="note-list">${item.notes
        .map((note) => `<li>${escapeHtml(note)}</li>`)
        .join("")}</ul>`
    : "";
  return `
    <details class="score-details">
      <summary>查看五项评分与来源</summary>
      <div class="detail-grid">${details}</div>
      ${notes}
      <p class="source-note"><strong>组合来源：</strong>${escapeHtml(item.source)}<br /><strong>注册备注：</strong>${escapeHtml(item.registryNote)}</p>
    </details>
  `;
}

function renderCandidateView() {
  const items = state.filteredCandidates;
  const totalPages = Math.max(1, Math.ceil(state.totalCandidates / state.candidatePageSize));
  const start = (state.candidatePage - 1) * state.candidatePageSize;
  const lengthLabel = state.length === "all" ? "所有长度" : `${state.length} 字`;
  elements.resultsTitle.textContent = state.characterFilter
    ? `包含“${state.characterFilter}”的候选`
    : state.initialCharacter
      ? `以“${state.initialCharacter}”开头的候选`
      : `按${SORT_MODES[state.sort].label}查看${lengthLabel}候选`;
  elements.sortStatus.textContent = SORT_MODES[state.sort].description;
  elements.resultsCount.textContent = `${state.totalCandidates.toLocaleString("zh-CN")} 条候选`;
  elements.emptyState.hidden = state.totalCandidates > 0;
  const hasCharacterFilter = state.initialCharacter || state.characterFilter;
  elements.initialFilterNotice.hidden = !hasCharacterFilter;
  if (hasCharacterFilter) {
    elements.initialFilterText.textContent = state.characterFilter
      ? `名称包含“${state.characterFilter}”`
      : `名称以“${state.initialCharacter}”开头`;
    elements.emptyState.querySelector("strong").textContent = state.characterFilter
      ? `没有包含“${state.characterFilter}”的候选`
      : `没有以“${state.initialCharacter}”开头的候选`;
    elements.emptyState.querySelector("p").textContent = "可以调整名字长度或生成新候选。";
  } else {
    elements.emptyState.querySelector("strong").textContent = "没有符合条件的名字";
    elements.emptyState.querySelector("p").textContent = "把最低分数调低，或清空搜索词继续查看。";
  }
  elements.namesList.innerHTML = items
    .map(
      (item, index) => `
        <article class="name-row">
          <div class="rank-number">${String(start + index + 1).padStart(2, "0")}</div>
          <div class="name-main">
            <div class="name-title-line">
              <strong class="name-title">${escapeHtml(item.name)}</strong>
              <span class="direction-tag">${item.name.length} 字 · ${escapeHtml(item.direction)}</span>
            </div>
            <p class="name-pitch">${escapeHtml(item.pitch)}</p>
            <div class="tag-list">${item.tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
          </div>
          <ul class="reason-list" aria-label="高分理由">
            ${item.reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}
          </ul>
          ${renderScore(item)}
          ${renderDimensionDetails(item)}
        </article>
      `,
    )
    .join("");
  candidateCardObserver.disconnect();
  [...elements.namesList.children].forEach((card) => candidateCardObserver.observe(card));
  scheduleCandidateLayout();
  elements.candidatePageLabel.textContent = `${state.candidatePage} / ${totalPages} 页`;
  elements.candidatePrev.disabled = state.candidatePage <= 1;
  elements.candidateNext.disabled = state.candidatePage >= totalPages;
  elements.candidatePagination.hidden = items.length === 0;
  if (state.candidatePage === 1) renderFeatured();
  else elements.featuredStrip.hidden = true;
}

function renderCharacterView() {
  const items = state.filteredCharacters;
  const totalPages = Math.max(1, Math.ceil(state.totalCharacters / state.characterPageSize));
  const start = (state.characterPage - 1) * state.characterPageSize;
  elements.resultsTitle.textContent = "完整汉字库";
  elements.resultsCount.textContent = `${state.totalCharacters.toLocaleString("zh-CN")} 个字库记录`;
  elements.characterGrid.innerHTML = items
    .map(
      (item) => `
        <button class="character-cell ${state.selectedCharacter === item.character ? "is-selected" : ""}" type="button" data-character="${escapeHtml(item.character)}" data-tier="${item.tier}" aria-label="围绕${escapeHtml(item.character)}生成候选；第 ${item.rank} 个字，${item.tier === 1 ? "一级常用字" : item.tier === 2 ? "二级规范字" : "三级规范字"}" aria-pressed="${state.selectedCharacter === item.character}" title="围绕“${escapeHtml(item.character)}”生成候选 · 第 ${item.rank} 个">
          <strong>${escapeHtml(item.character)}</strong>
          <span>第 ${item.rank}</span>
        </button>
      `,
    )
    .join("");
  elements.characterPageLabel.textContent = `${state.characterPage} / ${totalPages} 页`;
  elements.characterPrev.disabled = state.characterPage <= 1;
  elements.characterNext.disabled = state.characterPage >= totalPages;
  elements.characterPagination.hidden = items.length === 0;
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".view-tab").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === view);
    button.setAttribute("aria-selected", String(button.dataset.view === view));
  });
  elements.workspace.classList.toggle("character-mode", view === "characters");
  elements.candidateView.hidden = view !== "candidates";
  elements.charactersView.hidden = view !== "characters";
  elements.emptyState.hidden = view === "characters" || state.totalCandidates > 0;
  if (view === "characters") renderCharacterView();
  else renderCandidateView();
}

function selectCharacter(character, tier = state.filteredCharacters.find((item) => item.character === character)?.tier || 0) {
  state.selectedCharacter = character;
  state.selectedCharacterTier = Number(tier);
  elements.selectedCharacter.textContent = character;
  elements.characterGenerator.hidden = false;
  const supportedTier = state.selectedCharacterTier === 0 || state.selectedCharacterTier <= 2;
  elements.generateNamesButton.disabled = !state.aiConfigured || state.generating || !supportedTier;
  elements.viewCharacterCandidates.disabled = false;
  elements.generatorStatus.textContent = !supportedTier
    ? "三级规范字保留在完整字库中，当前 AI 生成只使用一、二级规范字。"
    : state.aiConfigured
      ? `AI 服务已导入${state.aiModel ? ` · ${state.aiModel}` : ""}`
      : "AI 服务尚未配置。请启动本地服务并导入 CC Switch 配置。";
  renderCharacterView();
  elements.characterGenerator.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function showCandidatesForCharacter() {
  state.characterFilter = state.selectedCharacter;
  state.initialCharacter = "";
  state.query = "";
  state.direction = "all";
  state.length = "all";
  state.minimumScore = 0;
  state.candidatePage = 1;
  elements.searchInput.value = "";
  elements.directionSelect.value = "all";
  elements.lengthSelect.value = "all";
  elements.scoreRange.value = "0";
  elements.scoreValue.textContent = "0";
  setView("candidates");
  applyCandidateFilters();
  elements.resultsTitle.scrollIntoView({ behavior: "smooth", block: "start" });
}

function clearInitialCharacterFilter() {
  state.initialCharacter = "";
  state.characterFilter = "";
  state.candidatePage = 1;
  applyCandidateFilters();
}

function populateDirections(directions) {
  elements.directionSelect.querySelectorAll("option:not([value='all'])").forEach((option) => option.remove());
  elements.directionSelect.insertAdjacentHTML(
    "beforeend",
    directions.map((direction) => `<option value="${escapeHtml(direction)}">${escapeHtml(direction)}</option>`).join(""),
  );
}

function exportCurrentNames() {
  const payload = state.filteredCandidates.map(({ id, name, leftToken, rightToken, template, direction, pitch, source, tags, scores, score, reasons, notes, registryNote }) => ({
    id,
    name,
    leftToken,
    rightToken,
    template,
    direction,
    pitch,
    source,
    tags,
    scores,
    totalScore: score,
    reasons,
    notes,
    registryNote,
  }));
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "名称候选-当前页.json";
  anchor.style.display = "none";
  document.body.appendChild(anchor);
  anchor.click();
  window.setTimeout(() => {
    anchor.remove();
    URL.revokeObjectURL(url);
  }, 1000);
}

function resetFilters() {
  state.query = "";
  state.direction = "all";
  state.minimumScore = 0;
  state.sort = "score";
  state.candidatePage = 1;
  state.initialCharacter = "";
  state.characterFilter = "";
  state.length = "all";
  elements.searchInput.value = "";
  elements.directionSelect.value = "all";
  elements.lengthSelect.value = "all";
  elements.scoreRange.value = "0";
  elements.scoreValue.textContent = "0";
  elements.sortSelect.value = "score";
  applyCandidateFilters();
}

function showLoadError(error) {
  console.error(error);
  elements.resultsCount.textContent = "读取失败";
  elements.emptyState.hidden = false;
  elements.emptyState.querySelector("strong").textContent = "本地服务不可用";
  elements.emptyState.querySelector("p").textContent = error.message || "请启动名字数据库服务后重试。";
}

async function loadStats() {
  state.stats = await apiRequest("/api/stats");
  updateThresholds(state.stats);
  populateDirections(state.stats.directions || []);
  renderStats();
}

async function loadAiStatus() {
  try {
    const [status, providersPayload] = await Promise.all([
      apiRequest("/api/ai/status"),
      apiRequest("/api/ai/providers"),
    ]);
    state.aiConfigured = Boolean(status.configured);
    state.aiModel = status.model || "";
    state.aiProviders = Array.isArray(providersPayload.providers) ? providersPayload.providers : [];
    state.aiProviderId = providersPayload.selectedProviderId || status.providerId || "";
    elements.generationProvider.innerHTML = state.aiProviders.length
      ? state.aiProviders.map((provider) => {
        const model = provider.model ? ` · ${provider.model}` : "";
        const protocol = provider.protocol ? ` · ${provider.protocol}` : "";
        return `<option value="${escapeHtml(provider.id)}">${escapeHtml(provider.name)}${escapeHtml(model)}${escapeHtml(protocol)}</option>`;
      }).join("")
      : '<option value="">没有可用的 CC Switch API 配置</option>';
    elements.generationProvider.disabled = state.aiProviders.length === 0;
    if (state.aiProviderId && state.aiProviders.some((provider) => provider.id === state.aiProviderId)) {
      elements.generationProvider.value = state.aiProviderId;
    } else if (state.aiProviders[0]) {
      state.aiProviderId = state.aiProviders[0].id;
      elements.generationProvider.value = state.aiProviderId;
    }
  } catch {
    state.aiConfigured = false;
    state.aiModel = "";
    state.aiProviders = [];
    state.aiProviderId = "";
    elements.generationProvider.innerHTML = '<option value="">CC Switch 配置不可用</option>';
    elements.generationProvider.disabled = true;
  }
}

async function generateForSelectedCharacter() {
  if (!state.selectedCharacter || !state.aiConfigured || state.selectedCharacterTier > 2 || state.generating) return;
  const character = state.selectedCharacter;
  const lengths = [...document.querySelectorAll("input[name='generationLength']:checked")]
    .map((input) => Number(input.value));
  if (!lengths.length) {
    elements.generatorStatus.textContent = "至少选择一种名字长度。";
    return;
  }

  state.generating = true;
  elements.generateNamesButton.disabled = true;
  elements.generatorStatus.textContent = `正在围绕“${character}”生成并评分…`;
  try {
    const result = await apiRequest("/api/generate/by-character", {
      method: "POST",
      body: JSON.stringify({
        character,
        count: Number(elements.generationCount.value),
        lengths,
        providerId: state.aiProviderId || elements.generationProvider.value || undefined,
      }),
    });
    elements.generatorStatus.textContent = `已生成 ${result.items.length} 个，新增入库 ${result.added} 个${result.model ? ` · ${result.model}` : ""}`;
    await loadStats();
    state.characterFilter = character;
    state.initialCharacter = "";
    state.query = "";
    state.direction = "all";
    state.length = "all";
    state.minimumScore = 0;
    state.candidatePage = 1;
    elements.searchInput.value = "";
    elements.directionSelect.value = "all";
    elements.lengthSelect.value = "all";
    elements.scoreRange.value = "0";
    elements.scoreValue.textContent = "0";
    setView("candidates");
    await refreshCandidates();
  } catch (error) {
    elements.generatorStatus.textContent = error.message || "生成失败，请检查本地 AI 配置后重试。";
  } finally {
    state.generating = false;
    elements.generateNamesButton.disabled = !state.aiConfigured || state.selectedCharacterTier > 2;
  }
}

async function init() {
  try {
    await Promise.all([loadStats(), loadAiStatus()]);
    // Browsers may restore the select value after a reload without firing change.
    state.sort = SORT_MODES[elements.sortSelect.value] ? elements.sortSelect.value : "score";
    applyCandidateFilters();
    applyCharacterFilters();
  } catch (error) {
    showLoadError(error);
  }
}

elements.searchInput.addEventListener("input", (event) => {
  state.query = event.target.value.trim();
  state.candidatePage = 1;
  applyCandidateFilters();
});

elements.directionSelect.addEventListener("change", (event) => {
  state.direction = event.target.value;
  state.candidatePage = 1;
  applyCandidateFilters();
});

elements.lengthSelect.addEventListener("change", (event) => {
  state.length = event.target.value;
  state.candidatePage = 1;
  applyCandidateFilters();
});

elements.scoreRange.addEventListener("input", (event) => {
  state.minimumScore = Number(event.target.value);
  elements.scoreValue.textContent = event.target.value;
  state.candidatePage = 1;
  applyCandidateFilters();
});

elements.sortSelect.addEventListener("change", (event) => {
  state.sort = event.target.value;
  state.candidatePage = 1;
  applyCandidateFilters();
});

elements.resetButton.addEventListener("click", resetFilters);
elements.clearInitialFilter.addEventListener("click", clearInitialCharacterFilter);
elements.exportButton.addEventListener("click", exportCurrentNames);
elements.candidatePrev.addEventListener("click", () => {
  state.candidatePage = Math.max(1, state.candidatePage - 1);
  applyCandidateFilters();
});
elements.candidateNext.addEventListener("click", () => {
  state.candidatePage += 1;
  applyCandidateFilters();
});
elements.characterPrev.addEventListener("click", () => {
  state.characterPage = Math.max(1, state.characterPage - 1);
  applyCharacterFilters();
});
elements.characterNext.addEventListener("click", () => {
  state.characterPage += 1;
  applyCharacterFilters();
});
elements.characterSearch.addEventListener("input", (event) => {
  state.characterQuery = event.target.value.trim();
  state.characterPage = 1;
  applyCharacterFilters();
});
elements.characterTierSelect.addEventListener("change", (event) => {
  state.characterTier = event.target.value;
  state.characterPage = 1;
  applyCharacterFilters();
});
document.querySelectorAll(".view-tab").forEach((button) => {
  button.addEventListener("click", () => setView(button.dataset.view));
});

elements.generateNamesButton.addEventListener("click", generateForSelectedCharacter);
elements.generationProvider.addEventListener("change", (event) => {
  state.aiProviderId = event.target.value;
  const provider = state.aiProviders.find((item) => item.id === state.aiProviderId);
  state.aiModel = provider?.model || "";
  if (state.selectedCharacter) selectCharacter(state.selectedCharacter);
});
elements.viewCharacterCandidates.addEventListener("click", showCandidatesForCharacter);

elements.characterGrid.addEventListener("click", (event) => {
  const characterButton = event.target.closest("[data-character]");
  if (characterButton) selectCharacter(characterButton.dataset.character, characterButton.dataset.tier);
});

init();
