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

const state = {
  candidates: [],
  characters: [],
  filteredCandidates: [],
  filteredCharacters: [],
  view: "candidates",
  query: "",
  direction: "all",
  minimumScore: 0,
  sort: "score",
  candidatePage: 1,
  characterPage: 1,
  characterQuery: "",
  characterTier: "all",
  candidatePageSize: 60,
  characterPageSize: 120,
};

const elements = {
  candidateCount: document.querySelector("#candidateCount"),
  characterCount: document.querySelector("#characterCount"),
  highScoreCount: document.querySelector("#highScoreCount"),
  combinationSpace: document.querySelector("#combinationSpace"),
  directionSelect: document.querySelector("#directionSelect"),
  searchInput: document.querySelector("#searchInput"),
  scoreRange: document.querySelector("#scoreRange"),
  scoreValue: document.querySelector("#scoreValue"),
  sortSelect: document.querySelector("#sortSelect"),
  resetButton: document.querySelector("#resetButton"),
  exportButton: document.querySelector("#exportButton"),
  resultsCount: document.querySelector("#resultsCount"),
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
};

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

function quantile(values, ratio) {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const index = Math.min(sorted.length - 1, Math.max(0, Math.floor(sorted.length * ratio)));
  return sorted[index];
}

function updateThresholds(candidates) {
  const totals = candidates.map((item) => item.score);
  // 前 10% 为优先研究，前 35% 为值得保留。
  thresholds.high = Math.round(quantile(totals, 0.9) * 10) / 10;
  thresholds.mid = Math.round(quantile(totals, 0.65) * 10) / 10;
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

function matchesCandidateQuery(item) {
  if (!state.query) return true;
  const haystack = [item.name, item.direction, item.pitch, item.source, item.leftToken, item.rightToken, ...item.tags]
    .join(" ")
    .toLowerCase();
  return haystack.includes(state.query.toLowerCase());
}

function sortCandidates(items) {
  const result = [...items];
  result.sort((a, b) => {
    if (state.sort === "name") return a.name.localeCompare(b.name, "zh-CN");
    if (state.sort === "score") return b.score - a.score || b.scores.distinctiveness - a.scores.distinctiveness || a.name.localeCompare(b.name, "zh-CN");
    return b.scores[state.sort] - a.scores[state.sort] || b.score - a.score || a.name.localeCompare(b.name, "zh-CN");
  });
  return result;
}

function applyCandidateFilters() {
  state.filteredCandidates = sortCandidates(
    state.candidates.filter(
      (item) =>
        (state.direction === "all" || item.direction === state.direction) &&
        item.score >= state.minimumScore &&
        matchesCandidateQuery(item),
    ),
  );
  const totalPages = Math.max(1, Math.ceil(state.filteredCandidates.length / state.candidatePageSize));
  state.candidatePage = Math.min(state.candidatePage, totalPages);
  renderCandidateView();
}

function applyCharacterFilters() {
  state.filteredCharacters = state.characters.filter((item) => {
    const matchesQuery = !state.characterQuery || item.character.includes(state.characterQuery);
    const matchesTier = state.characterTier === "all" || String(item.tier) === state.characterTier;
    return matchesQuery && matchesTier;
  });
  const totalPages = Math.max(1, Math.ceil(state.filteredCharacters.length / state.characterPageSize));
  state.characterPage = Math.min(state.characterPage, totalPages);
  if (state.view === "characters") renderCharacterView();
}

function renderStats() {
  elements.candidateCount.textContent = state.candidates.length.toLocaleString("zh-CN");
  elements.characterCount.textContent = state.characters.length.toLocaleString("zh-CN");
  elements.highScoreCount.textContent = state.candidates.filter((item) => item.score >= thresholds.high).length.toLocaleString("zh-CN");
  elements.combinationSpace.textContent = "1.785e15";
  elements.combinationSpace.title = "去除三级规范字后：6500^4 = 1,785,062,500,000,000；完整字库：8105^4 = 4,315,310,765,550,625";
}

function renderFeatured() {
  const featured = state.filteredCandidates.slice(0, 3);
  elements.featuredStrip.innerHTML = featured
    .map(
      (item, index) => `
        <article class="featured-item">
          <div class="featured-rank"><span>第 ${index + 1} 名</span><span>${item.score.toFixed(1)} / 100</span></div>
          <strong class="featured-name">${escapeHtml(item.name)}</strong>
          <span class="featured-meta">${escapeHtml(item.direction)} · ${escapeHtml(item.pitch)}</span>
        </article>
      `,
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
  const totalPages = Math.max(1, Math.ceil(items.length / state.candidatePageSize));
  const start = (state.candidatePage - 1) * state.candidatePageSize;
  const pageItems = items.slice(start, start + state.candidatePageSize);
  elements.resultsCount.textContent = `${items.length.toLocaleString("zh-CN")} 个规则候选`;
  elements.emptyState.hidden = items.length > 0;
  elements.namesList.innerHTML = pageItems
    .map(
      (item, index) => `
        <article class="name-row">
          <div class="rank-number">${String(start + index + 1).padStart(2, "0")}</div>
          <div class="name-main">
            <div class="name-title-line">
              <strong class="name-title">${escapeHtml(item.name)}</strong>
              <span class="direction-tag">${escapeHtml(item.direction)}</span>
            </div>
            <p class="name-pitch">${escapeHtml(item.pitch)}</p>
            <div class="tag-list">${item.tags.map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
          </div>
          <div class="reason-list" aria-label="高分理由">
            ${item.reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}
          </div>
          <div class="score-cell">
            <strong class="score-number ${scoreClass(item.score)}">${item.score.toFixed(1)}</strong>
            <span class="score-label">${scoreLabel(item.score)}</span>
          </div>
          ${renderDimensionDetails(item)}
        </article>
      `,
    )
    .join("");
  elements.candidatePageLabel.textContent = `${state.candidatePage} / ${totalPages} 页`;
  elements.candidatePrev.disabled = state.candidatePage <= 1;
  elements.candidateNext.disabled = state.candidatePage >= totalPages;
  elements.candidatePagination.hidden = items.length === 0;
  if (state.candidatePage === 1) renderFeatured();
  else elements.featuredStrip.hidden = true;
}

function renderCharacterView() {
  const items = state.filteredCharacters;
  const totalPages = Math.max(1, Math.ceil(items.length / state.characterPageSize));
  const start = (state.characterPage - 1) * state.characterPageSize;
  const pageItems = items.slice(start, start + state.characterPageSize);
  elements.resultsCount.textContent = `${items.length.toLocaleString("zh-CN")} 个字库记录`;
  elements.characterGrid.innerHTML = pageItems
    .map(
      (item) => `
        <div class="character-cell" title="第 ${item.rank} 个 · ${item.tier === 1 ? "一级常用字" : item.tier === 2 ? "二级规范字" : "三级规范字"}">
          <strong>${escapeHtml(item.character)}</strong>
          <span>第 ${item.rank}</span>
        </div>
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
  elements.emptyState.hidden = view === "characters" || state.filteredCandidates.length > 0;
  if (view === "characters") renderCharacterView();
  else renderCandidateView();
}

function populateDirections() {
  const directions = [...new Set(state.candidates.map((item) => item.direction))].sort((a, b) => a.localeCompare(b, "zh-CN"));
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
  anchor.download = "规则候选-评分结果.json";
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
  elements.searchInput.value = "";
  elements.directionSelect.value = "all";
  elements.scoreRange.value = "0";
  elements.scoreValue.textContent = "0";
  elements.sortSelect.value = "score";
  applyCandidateFilters();
}

async function init() {
  try {
    const [candidateResponse, characterResponse] = await Promise.all([
      fetch("data/candidates.json"),
      fetch("data/characters-8105.json"),
    ]);
    if (!candidateResponse.ok || !characterResponse.ok) throw new Error("database fetch failed");
    state.candidates = enrichCandidates(await candidateResponse.json());
    state.characters = (await characterResponse.json()).map((character, index) => ({
      character,
      rank: index + 1,
      tier: index < 3500 ? 1 : index < 6500 ? 2 : 3,
    }));
    updateThresholds(state.candidates);
    populateDirections();
    renderStats();
    applyCandidateFilters();
    applyCharacterFilters();
  } catch (error) {
    console.error(error);
    elements.resultsCount.textContent = "读取失败";
    elements.emptyState.hidden = false;
    elements.emptyState.querySelector("strong").textContent = "数据库读取失败";
    elements.emptyState.querySelector("p").textContent = "请通过本地 HTTP 服务打开此页面，而不是直接双击 HTML 文件。";
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
elements.exportButton.addEventListener("click", exportCurrentNames);
elements.candidatePrev.addEventListener("click", () => {
  state.candidatePage = Math.max(1, state.candidatePage - 1);
  renderCandidateView();
});
elements.candidateNext.addEventListener("click", () => {
  state.candidatePage += 1;
  renderCandidateView();
});
elements.characterPrev.addEventListener("click", () => {
  state.characterPage = Math.max(1, state.characterPage - 1);
  renderCharacterView();
});
elements.characterNext.addEventListener("click", () => {
  state.characterPage += 1;
  renderCharacterView();
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

init();
