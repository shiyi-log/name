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
  savedAiProviders: [],
  aiDraftId: "",
  aiDraftHasKey: false,
  aiDraftManaged: false,
  generating: false,
  autoGeneratingCharacter: "",
  autoGenerationMessage: "",
  view: "candidates",
  query: "",
  direction: "all",
  length: "all",
  minimumScore: 0,
  sort: "score",
  candidatePage: 1,
  candidatesLoading: false,
  candidateHasMore: false,
  candidateCursor: null,
  waterfallScrollArmed: false,
  waterfallAutoBlocked: false,
  totalCandidates: 0,
  totalCharacters: 0,
  charactersLoading: false,
  renderedCharacterCount: 0,
  characterColumns: [],
  characterQuery: "",
  characterTier: "all",
  initialCharacter: "",
  characterFilter: "",
  selectedCharacter: "",
  selectedCharacterTier: 0,
  candidatePageSize: 60,
  characterBatchSize: 240,
  candidateRequest: 0,
  rebuildingCandidates: false,
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
  generateForCharacterFromNotice: document.querySelector("#generateForCharacterFromNotice"),
  featuredStrip: document.querySelector("#featuredStrip"),
  namesList: document.querySelector("#namesList"),
  emptyState: document.querySelector("#emptyState"),
  automaticGenerationStatus: document.querySelector("#automaticGenerationStatus"),
  workspace: document.querySelector("#workspace"),
  candidateView: document.querySelector("#candidateView"),
  charactersView: document.querySelector("#charactersView"),
  candidatePagination: document.querySelector("#candidatePagination"),
  candidateLoadSentinel: document.querySelector("#candidateLoadSentinel"),
  candidateLoadStatus: document.querySelector("#candidateLoadStatus"),
  candidatePrev: document.querySelector("#candidatePrev"),
  candidateNext: document.querySelector("#candidateNext"),
  candidatePageLabel: document.querySelector("#candidatePageLabel"),
  characterGrid: document.querySelector("#characterGrid"),
  characterSearch: document.querySelector("#characterSearch"),
  characterTierSelect: document.querySelector("#characterTierSelect"),
  characterLoadSentinel: document.querySelector("#characterLoadSentinel"),
  characterLoadStatus: document.querySelector("#characterLoadStatus"),
  characterGenerator: document.querySelector("#characterGenerator"),
  selectedCharacter: document.querySelector("#selectedCharacter"),
  generationCount: document.querySelector("#generationCount"),
  generationProvider: document.querySelector("#generationProvider"),
  generateNamesButton: document.querySelector("#generateNamesButton"),
  viewCharacterCandidates: document.querySelector("#viewCharacterCandidates"),
  generatorStatus: document.querySelector("#generatorStatus"),
  appMain: document.querySelector("#top"),
  aiSettingsPage: document.querySelector("#aiSettingsPage"),
  openAiSettings: document.querySelector("#openAiSettings"),
  closeAiSettings: document.querySelector("#closeAiSettings"),
  savedProviderSelect: document.querySelector("#savedProviderSelect"),
  addAiProvider: document.querySelector("#addAiProvider"),
  aiProviderForm: document.querySelector("#aiProviderForm"),
  providerFormTitle: document.querySelector("#providerFormTitle"),
  providerSavedState: document.querySelector("#providerSavedState"),
  providerName: document.querySelector("#providerName"),
  providerBaseUrl: document.querySelector("#providerBaseUrl"),
  providerApiKey: document.querySelector("#providerApiKey"),
  apiKeyHint: document.querySelector("#apiKeyHint"),
  providerWireApi: document.querySelector("#providerWireApi"),
  providerModel: document.querySelector("#providerModel"),
  providerModelManual: document.querySelector("#providerModelManual"),
  refreshAiModels: document.querySelector("#refreshAiModels"),
  testAiConnection: document.querySelector("#testAiConnection"),
  aiProviderFormStatus: document.querySelector("#aiProviderStatus"),
};

// 保持页面元素按分数排序，再把每张卡片放入当前最短的一列。
// 这样展开详情时，后面的名次不会像多栏布局那样移到顶部。
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

let characterLayoutFrame = 0;
let characterLayoutStart = 0;
function scheduleCharacterLayout(startIndex = 0) {
  characterLayoutStart = characterLayoutFrame ? Math.min(characterLayoutStart, startIndex) : startIndex;
  if (characterLayoutFrame) return;
  characterLayoutFrame = window.requestAnimationFrame(() => {
    characterLayoutFrame = 0;
    const start = characterLayoutStart;
    characterLayoutStart = 0;
    layoutCharacterCards(start);
  });
}

function layoutCharacterCards(startIndex = 0) {
  const grid = elements.characterGrid;
  if (state.view !== "characters" || grid.clientWidth === 0) return;
  const cards = [...grid.children];
  const style = window.getComputedStyle(grid);
  const columns = style.gridTemplateColumns.split(" ").length;
  const columnGap = parseFloat(style.columnGap) || 0;
  const rowGap = parseFloat(style.rowGap) || 0;
  const cardWidth = (grid.clientWidth - columnGap * (columns - 1)) / columns;
  if (state.characterLayoutWidth !== grid.clientWidth || state.characterColumns.length !== columns) {
    startIndex = 0;
    state.characterLayoutWidth = grid.clientWidth;
    state.characterColumns = Array(columns).fill(0);
  }
  const columnHeights = state.characterColumns;

  for (let index = startIndex; index < cards.length; index += 1) {
    const card = cards[index];
    card.style.width = `${cardWidth}px`;
    const column = columnHeights.indexOf(Math.min(...columnHeights));
    card.style.transform = `translate(${column * (cardWidth + columnGap)}px, ${columnHeights[column]}px)`;
    columnHeights[column] += card.getBoundingClientRect().height + rowGap;
  }
  grid.style.height = `${Math.max(0, ...columnHeights) - (cards.length ? rowGap : 0) + 18}px`;
}

window.addEventListener("resize", scheduleCharacterLayout);
document.fonts.ready.then(scheduleCharacterLayout);

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

function candidateQuery(offset = state.characterFilter ? 0 : (state.candidatePage - 1) * state.candidatePageSize, after = null) {
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
  if (after) params.set("after", JSON.stringify(after));
  return `/api/candidates?${params}`;
}

async function refreshCandidates({ autoGenerateIfEmpty = false, append = false } = {}) {
  if (append && (state.candidatesLoading || !state.candidateHasMore)) return;
  const requestId = ++state.candidateRequest;
  const offset = append
    ? state.filteredCandidates.length
    : state.characterFilter ? 0 : (state.candidatePage - 1) * state.candidatePageSize;
  const previousCount = append ? state.filteredCandidates.length : 0;
  const cursor = append ? state.candidateCursor : null;
  state.candidatesLoading = true;
  if (!append) {
    state.filteredCandidates = [];
    state.totalCandidates = 0;
    state.candidateHasMore = false;
    state.candidateCursor = null;
    elements.namesList.replaceChildren();
    elements.namesList.style.height = "0px";
    candidateCardObserver.disconnect();
  }
  if (state.characterFilter) {
    elements.candidateLoadStatus.hidden = false;
    elements.candidateLoadStatus.textContent = append ? "正在加载更多候选…" : "正在读取候选…";
  }
  if (state.view === "candidates") elements.resultsCount.textContent = "读取中…";
  try {
    const payload = await apiRequest(candidateQuery(offset, cursor));
    if (requestId !== state.candidateRequest) return;
    const items = enrichCandidates(payload.items || []);
    if (append) {
      const seen = new Set(state.filteredCandidates.map((item) => item.id));
      state.filteredCandidates.push(...items.filter((item) => !seen.has(item.id)));
    }
    else state.filteredCandidates = items;
    state.totalCandidates = Number(payload.total || 0);
    state.candidateHasMore = Boolean(payload.hasMore);
    if (items.length) {
      const last = items.at(-1);
      state.candidateCursor = { name: last.name, score: last.score, scores: last.scores };
    }
    if (state.view === "candidates") renderCandidateView({ appendFrom: append ? previousCount : null });
    if (state.characterFilter) {
      const hasMore = state.candidateHasMore;
      elements.candidateLoadStatus.hidden = !hasMore;
      elements.candidateLoadStatus.textContent = hasMore
        ? `已显示 ${state.filteredCandidates.length.toLocaleString("zh-CN")} / ${state.totalCandidates.toLocaleString("zh-CN")} 条，继续滚动加载`
        : "已显示全部候选";
      elements.candidateLoadStatus.hidden = !hasMore && state.totalCandidates === 0;
    }
    if (autoGenerateIfEmpty && state.totalCandidates === 0 && state.characterFilter
      && state.autoGeneratingCharacter !== state.characterFilter) {
      state.autoGeneratingCharacter = state.characterFilter;
      await generateForSelectedCharacter({ automatic: true, endless: true });
    }
  } catch (error) {
    if (requestId !== state.candidateRequest) return;
    if (state.characterFilter) {
      elements.candidateLoadStatus.hidden = false;
      elements.candidateLoadStatus.textContent = `加载失败：${error.message}`;
    }
    if (state.view === "candidates") showLoadError(error);
  } finally {
    if (requestId === state.candidateRequest) {
      state.candidatesLoading = false;
      if (!state.rebuildingCandidates && append && state.candidateHasMore && state.view === "candidates"
        && elements.candidateLoadSentinel.getBoundingClientRect().top <= window.innerHeight + 500) {
        window.setTimeout(() => refreshCandidates({ append: true }), 0);
      } else if (!state.rebuildingCandidates && append && !state.candidateHasMore && state.waterfallScrollArmed) {
        maybeGenerateAtWaterfallEnd();
      }
    }
  }
}

async function reloadCandidateWindow(targetCount) {
  state.rebuildingCandidates = true;
  try {
    await refreshCandidates();
    while (state.filteredCandidates.length < targetCount && state.candidateHasMore) {
      await refreshCandidates({ append: true });
    }
  } finally {
    state.rebuildingCandidates = false;
  }
}

async function refreshCharacters() {
  const requestId = ++state.characterRequest;
  state.filteredCharacters = [];
  state.totalCharacters = 0;
  state.charactersLoading = false;
  state.renderedCharacterCount = 0;
  state.characterColumns = [];
  elements.characterGrid.replaceChildren();
  elements.characterGrid.style.height = "0px";
  elements.characterLoadStatus.textContent = "正在加载…";
  await loadMoreCharacters(requestId, 0, true);
}

async function loadMoreCharacters(requestId = state.characterRequest, offset = state.filteredCharacters.length, initial = false) {
  if (state.charactersLoading || requestId !== state.characterRequest) return;
  if (!initial && (state.totalCharacters === 0 || offset >= state.totalCharacters)) return;
  state.charactersLoading = true;
  let loaded = false;
  const params = new URLSearchParams({
    query: state.characterQuery,
    tier: state.characterTier,
    limit: String(state.characterBatchSize),
    offset: String(offset),
  });
  try {
    const payload = await apiRequest(`/api/characters?${params}`);
    if (requestId !== state.characterRequest) return;
    state.totalCharacters = Number(payload.total || 0);
    const items = payload.items || [];
    state.filteredCharacters.push(...items);
    loaded = true;
    if (state.view === "characters") renderCharacterView();
    elements.characterLoadStatus.textContent = state.filteredCharacters.length >= state.totalCharacters
      ? `已显示全部 ${state.totalCharacters.toLocaleString("zh-CN")} 个字`
      : `已显示 ${state.filteredCharacters.length.toLocaleString("zh-CN")} / ${state.totalCharacters.toLocaleString("zh-CN")} 个字`;
  } catch (error) {
    if (requestId !== state.characterRequest) return;
    if (state.view === "characters") {
      elements.characterLoadStatus.textContent = `加载失败：${error.message}`;
    }
  } finally {
    if (requestId === state.characterRequest) {
      state.charactersLoading = false;
      const sentinelTop = elements.characterLoadSentinel.getBoundingClientRect().top;
      const hasMore = state.filteredCharacters.length < state.totalCharacters;
      if (loaded && hasMore && state.view === "characters" && sentinelTop <= window.innerHeight + 500) {
        window.setTimeout(() => loadMoreCharacters(requestId), 0);
      }
    }
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
    ? `<p class="source-note"><strong>${item.sourceType === "literature" ? "文献取意说明" : "评分依据"}：</strong></p><ul class="note-list">${item.notes
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

function renderCandidateView({ appendFrom = null } = {}) {
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
  elements.automaticGenerationStatus.hidden = !state.characterFilter
    || state.autoGeneratingCharacter !== state.characterFilter || !state.autoGenerationMessage;
  elements.automaticGenerationStatus.textContent = state.autoGenerationMessage;
  elements.clearInitialFilter.textContent = state.characterFilter ? "返回字库" : "清除筛选";
  elements.generateForCharacterFromNotice.hidden = !state.characterFilter;
  const cards = items
    .slice(appendFrom ?? 0)
    .map(
      (item, index) => `
        <article class="name-row">
          <div class="rank-number">${String((appendFrom ?? start) + index + 1).padStart(2, "0")}</div>
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
  if (appendFrom === null) {
    elements.namesList.innerHTML = cards;
    candidateCardObserver.disconnect();
  } else {
    elements.namesList.insertAdjacentHTML("beforeend", cards);
  }
  [...elements.namesList.children].slice(appendFrom ?? 0).forEach((card) => candidateCardObserver.observe(card));
  scheduleCandidateLayout();
  elements.candidatePageLabel.textContent = `${state.candidatePage} / ${totalPages} 页`;
  elements.candidatePrev.disabled = state.candidatePage <= 1;
  elements.candidateNext.disabled = state.candidatePage >= totalPages;
  elements.candidatePagination.hidden = Boolean(state.characterFilter) || items.length === 0;
  elements.candidateLoadSentinel.hidden = !state.characterFilter;
  elements.candidateLoadStatus.hidden = !state.characterFilter || items.length === 0;
  if (state.candidatePage === 1) renderFeatured();
  else elements.featuredStrip.hidden = true;
}

function renderCharacterView() {
  const items = state.filteredCharacters;
  const startIndex = state.renderedCharacterCount;
  elements.resultsTitle.textContent = "完整汉字库";
  elements.resultsCount.textContent = `${state.totalCharacters.toLocaleString("zh-CN")} 个字库记录`;
  elements.characterGrid.insertAdjacentHTML("beforeend", items.slice(state.renderedCharacterCount)
    .map(
      (item) => `
        <button class="character-cell ${state.selectedCharacter === item.character ? "is-selected" : ""}" type="button" data-character="${escapeHtml(item.character)}" data-tier="${item.tier}" aria-label="围绕${escapeHtml(item.character)}生成候选；第 ${item.rank} 个字，${item.tier === 1 ? "一级常用字" : item.tier === 2 ? "二级规范字" : "三级规范字"}" aria-pressed="${state.selectedCharacter === item.character}" title="围绕“${escapeHtml(item.character)}”生成候选 · 第 ${item.rank} 个">
          <strong>${escapeHtml(item.character)}</strong>
          <span>第 ${item.rank}</span>
        </button>
      `,
    )
    .join(""));
  state.renderedCharacterCount = items.length;
  elements.characterGrid.querySelectorAll("[data-character]").forEach((button) => {
    const selected = button.dataset.character === state.selectedCharacter;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  scheduleCharacterLayout(startIndex);
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

function selectCharacter(character, tier = state.selectedCharacterTier) {
  state.selectedCharacter = character;
  state.selectedCharacterTier = Number(tier);
  elements.selectedCharacter.textContent = character;
  elements.characterGenerator.hidden = false;
  const supportedTier = state.selectedCharacterTier <= 2;
  elements.generateNamesButton.disabled = !state.aiConfigured || state.generating || !supportedTier;
  elements.viewCharacterCandidates.disabled = false;
  elements.generatorStatus.textContent = !supportedTier
    ? "三级规范字保留在完整字库中，当前 AI 生成只使用一、二级规范字。"
    : state.aiConfigured
    ? `AI 服务已配置${state.aiModel ? ` · ${state.aiModel}` : ""}`
    : "AI 服务尚未配置。请导入 CC Switch 配置，或设置 OPENAI_API_KEY 后启动本地服务。";
  elements.characterGrid.querySelectorAll("[data-character]").forEach((button) => {
    const selected = button.dataset.character === character;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
}

function openSelectedCharacterGenerator() {
  setView("characters");
  elements.characterGenerator.hidden = false;
  elements.characterGenerator.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function showCandidatesForCharacter({ autoGenerateIfEmpty = false } = {}) {
  if (state.characterFilter !== state.selectedCharacter) {
    state.autoGeneratingCharacter = "";
    state.autoGenerationMessage = "";
  }
  state.characterFilter = state.selectedCharacter;
  state.initialCharacter = "";
  state.query = "";
  state.direction = "all";
  state.length = "all";
  state.minimumScore = 0;
  state.candidatePage = 1;
  state.waterfallScrollArmed = false;
  state.waterfallAutoBlocked = false;
  elements.searchInput.value = "";
  elements.directionSelect.value = "all";
  elements.lengthSelect.value = "all";
  elements.scoreRange.value = "0";
  elements.scoreValue.textContent = "0";
  setView("candidates");
  refreshCandidates({ autoGenerateIfEmpty });
  elements.resultsTitle.scrollIntoView({ behavior: "smooth", block: "start" });
}

function clearInitialCharacterFilter() {
  if (state.characterFilter) {
    setView("characters");
    elements.resultsTitle.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
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
    state.aiProviders = Array.isArray(providersPayload.items)
      ? providersPayload.items.filter((provider) => state.savedAiProviders.some((saved) => saved.id === provider.id))
      : [];
    state.aiProviderId = providersPayload.selectedId || status.providerId || "";
    elements.generationProvider.innerHTML = state.aiProviders.length
      ? state.aiProviders.map((provider) => {
        const model = provider.model ? ` · ${provider.model}` : "";
        const protocol = provider.wireApi ? ` · ${provider.wireApi}` : "";
        return `<option value="${escapeHtml(provider.id)}">${escapeHtml(provider.name)}${escapeHtml(model)}${escapeHtml(protocol)}</option>`;
      }).join("")
      : '<option value="">没有可用的 AI 服务配置</option>';
    elements.generationProvider.disabled = state.aiProviders.length === 0;
    if (state.aiProviderId && state.aiProviders.some((provider) => provider.id === state.aiProviderId)) {
      elements.generationProvider.value = state.aiProviderId;
    } else if (state.aiProviders[0]) {
      state.aiProviderId = state.aiProviders[0].id;
      elements.generationProvider.value = state.aiProviderId;
    }
    const selected = state.aiProviders.find((provider) => provider.id === state.aiProviderId);
    state.aiConfigured = Boolean(selected);
    state.aiModel = selected?.model || "";
  } catch {
    state.aiConfigured = false;
    state.aiModel = "";
    state.aiProviders = [];
    state.aiProviderId = "";
    elements.generationProvider.innerHTML = '<option value="">AI 配置不可用</option>';
    elements.generationProvider.disabled = true;
  }
}

async function loadAiConfig() {
  const config = await apiRequest("/api/ai/config");
  state.savedAiProviders = Array.isArray(config.items) ? config.items : [];
  elements.savedProviderSelect.innerHTML = state.savedAiProviders.length
    ? state.savedAiProviders.map((provider) => `<option value="${escapeHtml(provider.id)}">${escapeHtml(provider.name)}${provider.id === config.selectedId ? " · 默认" : ""}</option>`).join("")
    : '<option value="">尚未保存服务商</option>';
  if (config.selectedId) elements.savedProviderSelect.value = config.selectedId;
  if (config.selectedId) editAiProvider(config.selectedId);
  await loadAiStatus();
}

function setAiSettingsVisible(visible) {
  elements.appMain.hidden = visible;
  elements.aiSettingsPage.hidden = !visible;
  document.querySelector(".hero-section").hidden = visible;
  document.querySelector(".stats-grid").hidden = visible;
  elements.workspace.hidden = visible;
  document.querySelector(".footer-note").hidden = visible;
  if (visible) {
    loadAiConfig().catch((error) => setAiProviderMessage(error.message, "error"));
  }
}

function setAiProviderMessage(message, stateName = "") {
  elements.aiProviderFormStatus.textContent = message;
  elements.aiProviderFormStatus.dataset.state = stateName;
}

function clearAiProviderForm() {
  state.aiDraftId = "";
  state.aiDraftHasKey = false;
  elements.aiProviderForm.reset();
  elements.providerFormTitle.textContent = "新增服务商";
  elements.providerSavedState.textContent = "";
  elements.apiKeyHint.textContent = "";
  elements.providerApiKey.required = true;
  elements.providerModel.innerHTML = '<option value="">可刷新模型或手动填写</option>';
  elements.providerModel.disabled = true;
  elements.providerModelManual.value = "";
  setAiProviderMessage("");
}

function editAiProvider(providerId) {
  const provider = state.savedAiProviders.find((item) => item.id === providerId);
  if (!provider) return clearAiProviderForm();
  state.aiDraftId = provider.id;
  state.aiDraftHasKey = Boolean(provider.apiKeyConfigured);
  state.aiDraftManaged = provider.managed !== false;
  elements.providerFormTitle.textContent = provider.name;
  elements.providerSavedState.textContent = provider.id === state.aiProviderId ? "当前默认" : "已保存";
  elements.providerName.value = provider.name;
  elements.providerBaseUrl.value = provider.baseUrl;
  elements.providerApiKey.value = "";
  elements.providerApiKey.required = !state.aiDraftHasKey;
  elements.apiKeyHint.textContent = state.aiDraftHasKey
    ? provider.managed === false ? "来自本机已有配置；保存后将复制到名字项目独立配置" : "Key 已保存；留空表示保留现有 Key"
    : "未保存 Key";
  elements.providerWireApi.value = provider.wireApi;
  fillAiModelOptions(provider.models || [], provider.model);
  elements.providerModelManual.value = provider.model || "";
  setAiProviderMessage("");
}

function fillAiModelOptions(models, selected = "") {
  const uniqueModels = [...new Set(models.filter((model) => typeof model === "string" && model.trim()))];
  elements.providerModel.innerHTML = uniqueModels.length
    ? uniqueModels.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("")
    : '<option value="">可手动填写模型 ID</option>';
  elements.providerModel.disabled = uniqueModels.length === 0;
  if (uniqueModels.includes(selected)) elements.providerModel.value = selected;
}

function aiProviderFormPayload() {
  const model = elements.providerModelManual.value.trim() || elements.providerModel.value;
  return {
    ...(state.aiDraftId ? { id: state.aiDraftId } : {}),
    name: elements.providerName.value.trim(),
    baseUrl: elements.providerBaseUrl.value.trim(),
    apiKey: elements.providerApiKey.value,
    wireApi: elements.providerWireApi.value,
    model: model.trim(),
    models: [...elements.providerModel.options].map((option) => option.value).filter(Boolean),
  };
}

async function saveAiProvider(event) {
  event.preventDefault();
  setAiProviderMessage("正在保存…");
  try {
    const saved = await apiRequest("/api/ai/config/save", { method: "POST", body: JSON.stringify(aiProviderFormPayload()) });
    await loadAiConfig();
    const provider = saved.items?.find((item) => item.id === saved.selectedId);
    if (provider) editAiProvider(provider.id);
    setAiProviderMessage("服务商已保存并设为默认", "success");
  } catch (error) {
    setAiProviderMessage(error.message, "error");
  }
}

async function refreshAiProviderModels() {
  setAiProviderMessage("正在读取模型列表…");
  try {
    const result = await apiRequest("/api/ai/config/models", { method: "POST", body: JSON.stringify(aiProviderFormPayload()) });
    fillAiModelOptions(result.models || [], result.model || "");
    elements.providerModelManual.value = result.model || "";
    setAiProviderMessage(result.models.length ? `找到 ${result.models.length} 个模型` : "服务可达，但没有返回模型；可手动填写模型 ID", "success");
  } catch (error) {
    setAiProviderMessage(error.message, "error");
  }
}

async function testAiProviderConnection() {
  setAiProviderMessage("正在检查连接…");
  try {
    const result = await apiRequest("/api/ai/config/test", { method: "POST", body: JSON.stringify(aiProviderFormPayload()) });
    setAiProviderMessage(`${result.message} · ${result.modelCount} 个模型可见`, "success");
  } catch (error) {
    setAiProviderMessage(error.message, "error");
  }
}

async function selectSavedAiProvider(providerId) {
  if (!providerId) return;
  try {
    await apiRequest("/api/ai/config/select", { method: "POST", body: JSON.stringify({ providerId }) });
    await loadAiConfig();
    editAiProvider(providerId);
    setAiProviderMessage("默认服务商已更新", "success");
  } catch (error) {
    setAiProviderMessage(error.message, "error");
  }
}

async function generateForSelectedCharacter({ automatic = false, endless = false } = {}) {
  if (!state.selectedCharacter || state.generating) return;
  const generationStatus = automatic ? elements.automaticGenerationStatus : elements.generatorStatus;
  if (!state.aiConfigured || state.selectedCharacterTier > 2) {
    const reason = !state.aiConfigured
      ? "AI 服务尚未配置，无法自动生成候选。"
      : "三级规范字暂不支持 AI 生成，请先查看其他字或调整筛选。";
    if (automatic) state.autoGenerationMessage = reason;
    if (automatic) state.waterfallAutoBlocked = true;
    generationStatus.hidden = false;
    generationStatus.textContent = reason;
    return;
  }
  const character = state.selectedCharacter;
  const previouslyVisibleCount = state.filteredCandidates.length;
  const lengths = [...document.querySelectorAll("input[name='generationLength']:checked")]
    .map((input) => Number(input.value));
  if (!lengths.length) {
    const message = "至少选择一种名字长度，无法开始生成。";
    generationStatus.hidden = false;
    generationStatus.textContent = message;
    if (automatic) state.autoGenerationMessage = message;
    if (automatic) state.waterfallAutoBlocked = true;
    return;
  }

  state.generating = true;
  elements.generateNamesButton.disabled = true;
  elements.generationProvider.disabled = true;
  generationStatus.hidden = false;
  if (automatic) state.autoGenerationMessage = endless
    ? `“${character}”暂无候选，正在生成一批…`
    : `候选已加载完，正在为“${character}”生成下一批…`;
  generationStatus.textContent = automatic ? state.autoGenerationMessage : `正在围绕“${character}”生成并评分…`;
  try {
    const result = await apiRequest("/api/generate/by-character", {
      method: "POST",
      body: JSON.stringify({
        character,
        count: Number(elements.generationCount.value),
        ...(endless ? { total_count: 200 } : {}),
        lengths,
        providerId: state.aiProviderId || elements.generationProvider.value || undefined,
      }),
    });
    generationStatus.textContent = automatic
      ? `${endless ? "自动生成完成" : "续生成完成"}：本批新增 ${result.added} 个${result.model ? ` · ${result.model}` : ""}`
      : `已生成 ${result.items.length} 个，新增入库 ${result.added} 个${result.model ? ` · ${result.model}` : ""}`;
    if (automatic) state.autoGenerationMessage = generationStatus.textContent;
    await loadStats();
    if (automatic) {
      state.characterFilter = character;
      state.initialCharacter = "";
      const visibleCount = endless
        ? Math.max(state.candidatePageSize, result.added)
        : previouslyVisibleCount + result.added;
      await reloadCandidateWindow(visibleCount);
      state.waterfallAutoBlocked = result.added === 0;
      elements.candidateLoadStatus.hidden = false;
      elements.candidateLoadStatus.textContent = result.added > 0
        ? `已显示 ${state.filteredCandidates.length.toLocaleString("zh-CN")} 条 · 继续向下滚动可生成下一批`
        : "本批没有新增候选，自动续生成已暂停";
    } else {
      if (result.added > 0) state.waterfallAutoBlocked = false;
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
    }
  } catch (error) {
    generationStatus.hidden = false;
    generationStatus.textContent = error.message || "生成失败，请检查本地 AI 配置后重试。";
    if (automatic) {
      state.autoGenerationMessage = generationStatus.textContent;
      state.waterfallAutoBlocked = true;
      elements.candidateLoadStatus.hidden = false;
      elements.candidateLoadStatus.textContent = "生成失败，自动续生成已暂停；可检查 AI 配置后重试";
    }
  } finally {
    state.generating = false;
    elements.generateNamesButton.disabled = !state.aiConfigured || state.selectedCharacterTier > 2;
    elements.generationProvider.disabled = state.aiProviders.length === 0;
  }
}

async function init() {
  try {
    await Promise.all([loadStats(), loadAiConfig()]);
    // 浏览器重新加载后可能恢复下拉框的值，但不会触发变更事件。
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
elements.generateForCharacterFromNotice.addEventListener("click", openSelectedCharacterGenerator);
elements.exportButton.addEventListener("click", exportCurrentNames);
elements.openAiSettings.addEventListener("click", () => setAiSettingsVisible(true));
elements.closeAiSettings.addEventListener("click", () => setAiSettingsVisible(false));
elements.addAiProvider.addEventListener("click", clearAiProviderForm);
elements.savedProviderSelect.addEventListener("change", (event) => {
  editAiProvider(event.target.value);
  selectSavedAiProvider(event.target.value);
});
elements.aiProviderForm.addEventListener("submit", saveAiProvider);
elements.refreshAiModels.addEventListener("click", refreshAiProviderModels);
elements.testAiConnection.addEventListener("click", testAiProviderConnection);
elements.providerModel.addEventListener("change", (event) => {
  elements.providerModelManual.value = event.target.value;
});

elements.candidatePrev.addEventListener("click", () => {
  state.candidatePage = Math.max(1, state.candidatePage - 1);
  applyCandidateFilters();
});
elements.candidateNext.addEventListener("click", () => {
  state.candidatePage += 1;
  applyCandidateFilters();
});

const candidateLoadObserver = new IntersectionObserver((entries) => {
  if (entries.some((entry) => entry.isIntersecting) && state.view === "candidates"
    && state.characterFilter && !state.candidatesLoading && !state.rebuildingCandidates) {
    if (state.candidateHasMore) refreshCandidates({ append: true });
    else maybeGenerateAtWaterfallEnd();
  }
}, { rootMargin: "600px 0px" });
candidateLoadObserver.observe(elements.candidateLoadSentinel);
let previousScrollY = window.scrollY;
function maybeGenerateAtWaterfallEnd() {
  if (!state.characterFilter || state.candidateHasMore || !state.waterfallScrollArmed || state.waterfallAutoBlocked
    || state.generating || state.candidatesLoading || state.view !== "candidates") return;
  if (elements.candidateLoadSentinel.getBoundingClientRect().top > window.innerHeight + 350) return;
  state.waterfallScrollArmed = false;
  generateForSelectedCharacter({ automatic: true });
}
window.addEventListener("wheel", (event) => {
  if (event.deltaY < 0 && state.characterFilter) {
    state.waterfallAutoBlocked = false;
    state.waterfallScrollArmed = false;
  }
  if (event.deltaY > 0 && state.characterFilter) {
    state.waterfallScrollArmed = true;
    maybeGenerateAtWaterfallEnd();
  }
}, { passive: true });
window.addEventListener("scroll", () => {
  const nextScrollY = window.scrollY;
  if (nextScrollY < previousScrollY && state.characterFilter) {
    state.waterfallAutoBlocked = false;
    state.waterfallScrollArmed = false;
  }
  if (nextScrollY > previousScrollY && state.characterFilter) {
    state.waterfallScrollArmed = true;
    maybeGenerateAtWaterfallEnd();
  }
  previousScrollY = nextScrollY;
}, { passive: true });
elements.characterSearch.addEventListener("input", (event) => {
  state.characterQuery = event.target.value.trim();
  applyCharacterFilters();
});
elements.characterTierSelect.addEventListener("change", (event) => {
  state.characterTier = event.target.value;
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
elements.viewCharacterCandidates.addEventListener("click", () => showCandidatesForCharacter({ autoGenerateIfEmpty: true }));

elements.characterGrid.addEventListener("click", (event) => {
  const characterButton = event.target.closest("[data-character]");
  if (characterButton) {
    selectCharacter(characterButton.dataset.character, characterButton.dataset.tier);
    showCandidatesForCharacter({ autoGenerateIfEmpty: true });
  }
});

const characterLoadObserver = new IntersectionObserver((entries) => {
  if (entries.some((entry) => entry.isIntersecting) && state.view === "characters") {
    loadMoreCharacters();
  }
}, { rootMargin: "500px 0px" });
characterLoadObserver.observe(elements.characterLoadSentinel);

init();
