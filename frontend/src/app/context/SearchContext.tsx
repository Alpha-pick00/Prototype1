import React, { createContext, useContext, useEffect, useState } from 'react';
import {
  checkClarifyFacets,
  decideStream,
  extractOcr,
  fetchServerHistory,
  saveServerHistory,
  deleteServerHistoryEntry,
  clearServerHistory,
  looksAmbiguous,
  recordPreference,
  ApiError,
  type DecideResult,
  type DecideStage,
  type Proposal,
  type ServerHistoryEntry,
} from '../lib/api';
import { getStoredToken } from '../lib/auth';
import { useAuth } from './AuthContext';
import {
  deleteHistoryEntry,
  clearHistory,
  loadHistory,
  saveHistoryEntry,
  type HistoryEntry,
} from '../lib/history';

const fromServerEntry = (entry: ServerHistoryEntry): HistoryEntry => ({
  id: entry.id,
  query: entry.query,
  timestamp: entry.timestamp * 1000, // 서버는 초 단위, 프론트는 Date.now() 기준 ms 단위로 통일
  result: entry.result,
});

export type TurnStatus = 'loading' | 'result' | 'error';

// ChatGPT/Claude 스타일 대화 스레드의 한 왕복(사용자 메시지 -> 검색 결과).
// displayQuery는 사용자 말풍선에 그대로 보여줄 텍스트, requestQuery는 실제로
// 서버에 던진 값 - facet 답변을 이어붙인 턴은 이 둘이 다르다.
export interface ChatTurn {
  id: string;
  displayQuery: string;
  requestQuery: string;
  // AI 상세검색 드릴다운 체인의 맨 처음 검색어(속도 개선, 2026-08-13) - "핸드폰"
  // -> "핸드폰 삼성전자"로 좁혀가는 동안 이 값은 계속 "핸드폰"으로 고정된다.
  // checkClarifyFacets가 이걸 base_query로 보내 백엔드 캐시를 재사용한다.
  baseQuery: string;
  // 다중 선택 지원(2026-08-24, "하나밖에 선택을 못하는데 여러개 선택할 수
  // 있게") - 드릴다운 체인 전체에 걸쳐 지금까지 고른 {facet 라벨: [값, ...]}를
  // 누적한다. requestQuery(문자열 이어붙이기)는 표시/캐시 재사용용으로 계속
  // 쓰지만, 실제 후보 필터링은 이 구조화된 값을 백엔드에 그대로 보내
  // OR/AND를 정확히 구분한다(문자열만으로는 "브랜드 A 또는 B"를 표현할 수
  // 없다).
  facetAnswers: Record<string, string[]>;
  status: TurnStatus;
  result: DecideResult | null;
  errorMessage: string;
  // 검색 진행 상태 - decideStream이 이 턴을 처리하는 동안 status/proposal
  // 이벤트로 채워진다.
  streamingStage: DecideStage | null;
  streamingProposals: Proposal[];
  // 메시지 시간 표시(사용자 요청, "클로드 너처럼 날짜기능") - epoch ms.
  // loadFromHistory는 실제 기록 시각을 쓰고, 그 외엔 턴 생성 시각.
  createdAt: number;
}

// ChatGPT/Gemini처럼 "창(대화)" 하나에 여러 턴이 계속 이어지고, 새 상품을 검색할
// 때는 새 창을 연다(2026-08-15, "새로운 상품을 검색할 때는 새로운 창을 띄워서
// 검색할 수 있게하고 하나의 창에서는 하나의 대화로그가 계속 이어질수 있도록").
// 세션 범위로만 유지한다(새로고침하면 사라짐) - 완료된 검색 결과 자체는
// 기존 history(HistoryEntry, 서버/로컬 영구 저장)가 이미 별도로 보존한다.
export interface Conversation {
  id: string;
  // 사이드바 목록에 보여줄 제목 - 이 대화의 첫 턴 displayQuery로 고정한다
  // (이후 턴이 쌓여도 안 바뀜, ChatGPT의 대화 제목과 같은 동작).
  title: string;
  turns: ChatTurn[];
  updatedAt: number;
}

interface SearchContextValue {
  turns: ChatTurn[];
  isBusy: boolean;
  ocrBusy: boolean;
  history: HistoryEntry[];
  // 사용자 페르소나(2026-08-15) - 이번 세션에서 지금까지 고른 {facet 라벨: 값}.
  // SearchResults가 옵션 버튼에 "선호" 표시를 붙이는 데 쓴다 - 실제 옵션 순서
  // 반영(우선순위를 앞으로 당기는 것)은 백엔드(check_clarify_facets)가 이미
  // 하므로, 여기서는 순수하게 시각적 표시 용도다.
  sessionPreferences: Record<string, string>;
  sendMessage: (q: string) => Promise<void>;
  selectFacets: (turnId: string, selected: Record<string, string[]>) => Promise<void>;
  // 조건을 하나도 안 고르고 원래 질의 그대로 포괄적으로 검색한다(2026-08-24).
  searchBroadly: (turnId: string) => Promise<void>;
  retryTurn: (turnId: string) => Promise<void>;
  editTurn: (turnId: string, newQuery: string) => Promise<void>;
  handleImageUpload: (file: File) => Promise<void>;
  handleReset: () => void;
  loadFromHistory: (entry: HistoryEntry) => void;
  deleteFromHistory: (id: string) => void;
  clearAllHistory: () => void;
}

const SearchContext = createContext<SearchContextValue | null>(null);

// facet/옵션 선택을 이어붙일 때 이미 있는 단어를 또 붙이지 않는다(사용자 요청,
// 2026-08-14: "다나와에서 '초코파이 오리온 초코파이 바나나 468g'에 대한 가격
// 정보를 찾지 못했다" - 시리즈 옵션 "초코파이 바나나" 자체가 이미 원래 검색어
// "초코파이"를 포함하고 있어서, 그냥 이어붙이면 "초코파이"가 두 번 들어가
// 검색이 이상하게 안 맞는 검색어가 됐다). 토큰(공백 기준) 단위로만 비교한다.
export const dedupeAppend = (base: string, addition: string): string => {
  const baseTokens = base.trim().split(/\s+/).filter(Boolean);
  const seen = new Set(baseTokens.map((t) => t.toLowerCase()));
  const newTokens = addition
    .trim()
    .split(/\s+/)
    .filter((t) => t && !seen.has(t.toLowerCase()));
  return [...baseTokens, ...newTokens].join(' ');
};

const newTurn = (
  displayQuery: string,
  requestQuery: string,
  baseQuery?: string,
  facetAnswers?: Record<string, string[]>
): ChatTurn => ({
  id: crypto.randomUUID(),
  displayQuery,
  requestQuery,
  baseQuery: baseQuery || requestQuery,
  facetAnswers: facetAnswers ?? {},
  status: 'loading',
  result: null,
  errorMessage: '',
  streamingStage: null,
  streamingProposals: [],
  createdAt: Date.now(),
});

export const SearchProvider = ({ children }: { children: React.ReactNode }) => {
  const { user } = useAuth();
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  const [ocrBusy, setOcrBusy] = useState(false);
  const [history, setHistory] = useState<HistoryEntry[]>(() => loadHistory());
  // 사용자 페르소나(2026-08-15, "냉장고 살 때랑 콜라 살 때 쓰는 메타데이터가
  // 다르다" -> "사용자 페르소나 기반으로 상품 매핑") - 이번 세션에서 지금까지
  // 고른 facet/축 값을 {라벨: 값}으로 누적한다. 대화(conversation) 하나에
  // 갇히지 않고 세션 전체(예: 폰 대화에서 "삼성" 고른 뒤, 완전히 새로 연
  // 이어폰 대화에도)에 걸쳐 유지된다. 로그인 계정에는 추가로 영구 저장한다
  // (rememberPreference 참고) - 세션 값은 새로고침하면 사라지지만, 계정 값은
  // 다음 방문에도 /decide/clarify가 자동으로 다시 불러와 반영한다.
  const [sessionPreferences, setSessionPreferences] = useState<Record<string, string>>({});

  const activeConversation = conversations.find((c) => c.id === activeConversationId) ?? null;
  const turns = activeConversation?.turns ?? [];

  // 로그인 상태가 바뀌면 기록 소스를 전환한다 — 로그인하면 그 계정의 서버 기록을
  // 불러오고, 로그아웃하면 이 브라우저의 로컬 기록으로 되돌아간다.
  useEffect(() => {
    if (!user) {
      setHistory(loadHistory());
      return;
    }
    const token = getStoredToken();
    if (!token) return;
    fetchServerHistory(token).then((entries) => setHistory(entries.map(fromServerEntry)));
  }, [user]);

  const persistHistoryEntry = async (q: string, data: DecideResult) => {
    if (user) {
      const token = getStoredToken();
      if (!token) return;
      const saved = await saveServerHistory(token, q, data);
      if (saved) {
        setHistory((prev) => [fromServerEntry(saved), ...prev]);
      }
      return;
    }
    setHistory(saveHistoryEntry(q, data));
  };

  // 턴 ID는 crypto.randomUUID()로 전역 유일하므로, 어느 대화가 활성 상태인지와
  // 무관하게 모든 대화를 뒤져 실제로 그 턴을 담고 있는 대화만 갱신한다 - 스트리밍
  // 응답이 도착하는 동안 사용자가 다른 대화로 전환해도 엉뚱한(현재 활성) 대화가
  // 아니라 turn이 실제로 속한 대화가 정확히 갱신된다.
  const patchTurn = (id: string, patch: Partial<ChatTurn>) => {
    setConversations((prev) =>
      prev.map((c) =>
        c.turns.some((t) => t.id === id)
          ? { ...c, turns: c.turns.map((t) => (t.id === id ? { ...t, ...patch } : t)), updatedAt: Date.now() }
          : c
      )
    );
  };

  const appendStreamingProposal = (turnId: string, proposal: Proposal) => {
    setConversations((prev) =>
      prev.map((c) =>
        c.turns.some((t) => t.id === turnId)
          ? {
              ...c,
              turns: c.turns.map((t) =>
                t.id === turnId ? { ...t, streamingProposals: [...t.streamingProposals, proposal] } : t
              ),
            }
          : c
      )
    );
  };

  const findTurn = (turnId: string): ChatTurn | undefined =>
    conversations.flatMap((c) => c.turns).find((t) => t.id === turnId);

  // 새 턴을 대화에 붙인다 - conversationId가 없으면(새 대화의 첫 턴) 새 대화를
  // 만들어 목록 맨 앞에 얹고 활성으로 전환한다. 이게 "새로운 상품을 검색할 때는
  // 새로운 창"의 실제 진입점: handleReset이 activeConversationId를 null로
  // 돌려놓으면, 다음 sendMessage가 이 분기를 타 자동으로 새 창을 연다.
  const appendTurn = (conversationId: string | null, turn: ChatTurn) => {
    if (conversationId) {
      setConversations((prev) =>
        prev.map((c) =>
          c.id === conversationId ? { ...c, turns: [...c.turns, turn], updatedAt: Date.now() } : c
        )
      );
      return conversationId;
    }
    const id = crypto.randomUUID();
    setConversations((prev) => [{ id, title: turn.displayQuery, turns: [turn], updatedAt: Date.now() }, ...prev]);
    setActiveConversationId(id);
    return id;
  };

  // 페르소나 한 줄(라벨:값) 기록 - 세션 상태는 즉시 반영해 바로 다음
  // checkClarifyFacets 호출부터 쓰이고, 로그인 계정에는 fire-and-forget으로
  // 영구 저장한다(실패해도 검색 흐름에 영향 없음).
  const rememberPreference = (label: string, value: string) => {
    setSessionPreferences((prev) => ({ ...prev, [label]: value }));
    if (user) {
      const token = getStoredToken();
      if (token) recordPreference(token, label, value).catch(() => {});
    }
  };

  // 턴 하나의 실제 검색/조회를 실행하고 그 턴만 갱신한다. sendMessage(새 턴 추가)/
  // selectFacets(후속 턴 추가)/retryTurn(기존 턴 재실행) 전부 이 위에서 돈다.
  // decideStream이 이 함수 안쪽(백엔드 API 호출)에서 한 턴을 처리하는
  // stateless 단계를 맡는다 - 턴/대화/baseQuery 관리는 이 함수 바깥
  // (sendMessage 등 호출부)의 책임이다.
  //
  // skipIntentCheck - 이전 턴에서 축적된 검색어로 이어가는 드릴다운 후속 턴
  // (requestQuery !== baseQuery)이면 아래 looksAmbiguous 재질문 체크를
  // 건너뛴다 - 안 그러면 이미 한 번 답한 축(용량 등)에 대해 또 clarify를
  // 띄우는 재질문 버그가 생긴다.
  //
  // personaOverride - 바로 이 턴을 만든 선택(예: 방금 rememberPreference로 기록한
  // 라벨:값)을 checkClarifyFacets 호출에 즉시 반영하기 위한 값이다. setState는
  // 비동기라 rememberPreference 직후 곧바로 runTurn을 불러도 이 함수가 캡처한
  // sessionPreferences는 아직 리렌더 전 값(반영 안 됨)일 수 있어, 호출부가 방금
  // 배운 값을 명시적으로 얹어 보낸다.
  const runTurn = async (
    id: string,
    requestQuery: string,
    baseQuery?: string,
    personaOverride?: Record<string, string>,
    facetAnswers?: Record<string, string[]>,
    forceSkipClarify?: boolean
  ) => {
    // forceSkipClarify(2026-08-24, "카테고리를 선택해야지만 검색할 수 있는데
    // ... 선택 안해도 그냥 바로 포괄적으로도 검색 가능하게") - AI 상세검색
    // 카드에서 "그냥 검색하기"를 누른 턴이다. requestQuery === baseQuery라
    // 아래 skipIntentCheck 추론(문자열 비교)만으로는 "이미 한 축 답한 후속
    // 턴"과 구분이 안 된다 - 명시적으로 이번 한 번만 애매함 체크를 건너뛴다.
    const skipIntentCheck = forceSkipClarify || requestQuery !== (baseQuery ?? requestQuery);

    try {
      // AI 상세검색(2026-08-12) - "음료수"처럼 짧고 애매한 검색어면 11번가 실측
      // 가격 스트림을 바로 태우기 전에 먼저 물어본다. looksAmbiguous()가
      // 대부분의(구체적인) 검색어를 걸러내므로 이 호출 자체가 거의 항상 스킵된다.
      // 실패해도(.catch) 조용히 원래 검색으로 넘어간다 - AI 상세검색은 있으면
      // 좋은 보조 기능이지 필수 경로가 아니다. 후속 턴(skipIntentCheck)에서는
      // 이미 한 축을 답했으므로 다시 묻지 않는다.
      // 중복 정제 호출 제거(2026-08-25, lsm 브랜치에서 이식) - checkClarifyFacets가
      // 이미 대화체 질의를 정제해서 clarify.query로 돌려주는데(app/debate.py::
      // check_clarify_facets의 _maybe_refine_query), facets가 비어 원래 검색으로
      // 넘어갈 때 requestQuery(정제 전 원문)를 그대로 decideStream에 보내면
      // run_elevenst_only_debate_stream 내부의 _maybe_refine_query가 같은 질의를
      // 또 한 번 LLM으로 정제한다 - 이미 정제된 clarify.query를 재사용하면
      // looks_conversational_query가 더 이상 안 걸려(정제 결과엔 "사고싶어" 같은
      // 구매 의도 문구가 안 남음) 이 두 번째 호출이 자연히 생략된다.
      //
      // effectiveBaseQuery도 같이 갱신해야 한다 - 첫 턴엔 baseQuery가 requestQuery와
      // 같은 문자열인데(newTurn 참고), query만 갱신하고 baseQuery는 원문 그대로
      // 보내면 서버의 _refine_base_query가 "base_query !== original_query"로 오판해
      // (원문 vs 정제문 비교가 되어버림) base_query를 또 한 번 별도로 정제해버린다 -
      // 결국 호출 위치만 바뀔 뿐 LLM 호출 수는 그대로다. 서버와 같은 조건
      // (drilldown 전엔 baseQuery===requestQuery)으로 여기서도 같이 정제문으로
      // 맞춰줘야 진짜로 호출이 하나 줄어든다. 진짜 드릴다운(baseQuery가 원래부터
      // requestQuery와 다름)이면 그대로 둔다 - 서버가 그 base_query를 알아서 정제한다.
      let searchQuery = requestQuery;
      let effectiveBaseQuery = baseQuery;
      if (!skipIntentCheck && looksAmbiguous(requestQuery)) {
        const persona = { ...sessionPreferences, ...personaOverride };
        const clarify = await checkClarifyFacets(
          requestQuery,
          baseQuery,
          persona,
          getStoredToken(),
          facetAnswers
        ).catch(() => null);
        if (clarify && clarify.options.facets.length > 0) {
          patchTurn(id, { status: 'result', result: clarify });
          return;
        }
        if (clarify) {
          searchQuery = clarify.query;
          if (baseQuery && baseQuery.trim() === requestQuery.trim()) {
            effectiveBaseQuery = clarify.query;
          }
        }
      }

      let finalResult: DecideResult | null = null;
      let streamError: string | null = null;
      await decideStream(
        searchQuery,
        (event) => {
          if (event.type === 'status') {
            patchTurn(id, { streamingStage: event.stage });
          } else if (event.type === 'proposal') {
            appendStreamingProposal(id, event.proposal);
          } else if (event.type === 'final') {
            // 체감 속도 개선(2026-08-24) - 메인 추천이 끝나는 대로 바로
            // 화면에 반영한다("다른 후보"의 개별 이유는 아직 일반 문구일 수
            // 있음 - notes 이벤트가 오면 마저 채워진다). 예전엔 스트림이 다
            // 끝날 때까지 화면 반영 자체를 미뤘었다.
            finalResult = event.result;
            patchTurn(id, { status: 'result', result: event.result });
          } else if (event.type === 'notes') {
            // "다른 후보" 개별 이유가 뒤늦게 도착 - 이미 그려둔 카드의 이유
            // 텍스트만 갈아끼운다(상품명/가격/이미지는 final에서 이미 확정).
            if (finalResult && finalResult.mode === 'single') {
              finalResult = { ...finalResult, proposals: event.proposals };
              patchTurn(id, { result: finalResult });
            }
          } else if (event.type === 'error') {
            streamError = event.message;
          }
        },
        undefined,
        effectiveBaseQuery,
        facetAnswers
      );

      if (streamError || !finalResult) {
        throw new ApiError(streamError || '요청 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요.');
      }
      persistHistoryEntry(requestQuery, finalResult).catch(() => {});
    } catch (err) {
      patchTurn(id, {
        status: 'error',
        errorMessage:
          err instanceof ApiError ? err.message : '요청 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요.',
      });
    }
  };

  // 활성 대화가 있으면 그 대화에 이어붙이고(ChatGPT에서 입력창에 타이핑하는 것과
  // 동일), 없으면(방금 새 검색을 눌렀거나 첫 방문) 새 대화를 연다.
  const sendMessage = async (q: string) => {
    const trimmed = q.trim();
    if (!trimmed) return;
    const turn = newTurn(trimmed, trimmed);
    appendTurn(activeConversationId, turn);
    await runTurn(turn.id, turn.requestQuery, turn.baseQuery);
  };

  // AI 상세검색 카드에서 기준(facet) 옵션을 하나 고르면 원래 검색어 뒤에 덧붙여
  // 같은 대화에 새 메시지처럼 이어붙인다. 조합한 검색어가 여전히 애매하면
  // runTurn의 clarify 선체크가 다시 걸려 자연스럽게 여러 턴에 걸쳐 좁혀나갈 수 있다.
  // baseQuery는 origin에서 그대로 물려받는다(origin.requestQuery가 아니라) -
  // 드릴다운 체인 전체가 맨 처음 검색어 하나로 고정돼야 백엔드가 매번 그
  // 하나만 캐시해서 재사용할 수 있다(속도 개선, 2026-08-13).
  //
  // selected가 {라벨: [값, ...]}으로 넘어온다(다중 선택 지원, 2026-08-24 - 이전엔
  // 라벨당 값 하나였는데, "하나밖에 선택을 못하는데 여러개 선택할 수 있게"
  // 요청으로 배열이 됐다). 라벨을 같이 받아야 어느 facet에서 이 값들을
  // 골랐는지 세션/계정 페르소나에 정확히 기록하고, 구조화된 facet_answers로
  // 백엔드에 OR/AND 필터링을 정확히 전달할 수 있다.
  const selectFacets = async (turnId: string, selected: Record<string, string[]>) => {
    const origin = findTurn(turnId);
    const conversation = conversations.find((c) => c.turns.some((t) => t.id === turnId));
    const allValues = Object.values(selected).flat();
    if (!origin || !conversation || allValues.length === 0) return;
    Object.entries(selected).forEach(([label, values]) =>
      values.forEach((value) => rememberPreference(label, value))
    );
    const combined = allValues.reduce((acc, value) => dedupeAppend(acc, value), origin.requestQuery).trim();
    // 2026-08-18(사용자 리포트: "핸드폰 한다음에 샤오미 넣었는데 샤오미만 다시
    // 검색되는게 뭐하는거야 '핸드폰 샤오미' 이렇게 전에 했던것도 붙여서 넣어야지")
    // - 실제로 백엔드에 보내는 requestQuery(=combined)는 이미 이전 검색어까지
    // 합쳐져 있었지만, 말풍선에 보여주는 displayQuery는 방금 고른 값만
    // (values.join)이라 마치 이전 맥락이 사라진 것처럼 보였다. 실제 검색어와
    // 화면 표시를 일치시킨다.
    //
    // facetAnswers 누적(2026-08-24) - 이전 턴까지 쌓인 값에 이번 턴에서 고른
    // 값을 라벨별로 합친다(중복 제거) - 드릴다운이 여러 라운드에 걸쳐 이어져도
    // 구조화된 필터가 안 끊긴다.
    const accumulatedFacetAnswers: Record<string, string[]> = { ...origin.facetAnswers };
    for (const [label, values] of Object.entries(selected)) {
      const existing = accumulatedFacetAnswers[label] ?? [];
      accumulatedFacetAnswers[label] = Array.from(new Set([...existing, ...values]));
    }
    const turn = newTurn(combined, combined, origin.baseQuery, accumulatedFacetAnswers);
    appendTurn(conversation.id, turn);
    const personaOverride: Record<string, string> = {};
    for (const [label, values] of Object.entries(selected)) {
      if (values.length > 0) personaOverride[label] = values[values.length - 1];
    }
    await runTurn(turn.id, turn.requestQuery, turn.baseQuery, personaOverride, accumulatedFacetAnswers);
  };

  // AI 상세검색 카드에서 조건을 하나도 안 고르고 "그냥 검색하기"를 누르면
  // 호출된다(2026-08-24, "선택 안해도 그냥 바로 포괄적으로도 검색 가능하게
  // 하고 싶어") - 원래 질의 그대로(축 하나도 안 좁힌 채) 검색한다. selectFacets
  // 와 달리 값이 하나도 없어도 진행해야 하고, runTurn의 애매함 재체크를
  // forceSkipClarify로 명시적으로 건너뛴다 - 안 그러면 같은 질의로 다시 물어
  // 똑같은 clarify 카드가 반복돼 사용자가 절대 벗어날 수 없다.
  const searchBroadly = async (turnId: string) => {
    const origin = findTurn(turnId);
    const conversation = conversations.find((c) => c.turns.some((t) => t.id === turnId));
    if (!origin || !conversation) return;
    const turn = newTurn(origin.requestQuery, origin.requestQuery, origin.baseQuery, origin.facetAnswers);
    appendTurn(conversation.id, turn);
    await runTurn(turn.id, turn.requestQuery, turn.baseQuery, undefined, origin.facetAnswers, true);
  };

  const retryTurn = async (turnId: string) => {
    const turn = findTurn(turnId);
    if (!turn) return;
    patchTurn(turnId, { status: 'loading', errorMessage: '', streamingStage: null, streamingProposals: [] });
    await runTurn(turnId, turn.requestQuery, turn.baseQuery, undefined, turn.facetAnswers);
  };

  // 내 메시지 편집(사용자 요청, "클로드 너처럼 ... 편집기능") - 클로드처럼 편집한
  // 턴 이후에 이어지던 턴들은 그 편집 전 맥락으로 답한 것이라 더 이상 유효하지
  // 않으므로 버리고, 편집한 턴을 새 루트 질문 취급해 처음부터 다시 실행한다.
  // id는 그대로 유지해 리스트에서 자리가 안 바뀌게 한다.
  const editTurn = async (turnId: string, newQuery: string) => {
    const trimmed = newQuery.trim();
    if (!trimmed) return;
    const conversation = conversations.find((c) => c.turns.some((t) => t.id === turnId));
    if (!conversation) return;
    const index = conversation.turns.findIndex((t) => t.id === turnId);
    if (index === -1) return;
    const edited: ChatTurn = { ...newTurn(trimmed, trimmed), id: turnId };
    setConversations((prev) =>
      prev.map((c) =>
        c.id === conversation.id
          ? { ...c, turns: [...c.turns.slice(0, index), edited], updatedAt: Date.now() }
          : c
      )
    );
    await runTurn(edited.id, edited.requestQuery, edited.baseQuery);
  };

  const handleImageUpload = async (file: File) => {
    setOcrBusy(true);
    try {
      const { ocr, cleaned } = await extractOcr(file);

      if (cleaned?.search_query?.trim()) {
        await sendMessage(cleaned.search_query.trim());
        return;
      }

      // Groq 정제가 정상적으로 돌았는데도(에러 없음) search_query가 빈
      // 문자열이면, LLM이 "상품을 하나로 특정할 수 없다"고 일부러 포기한
      // 것이다(cleanup.py 프롬프트: "상품을 특정할 수 없으면 search_query를
      // 빈 문자열로 두세요" - 지어내지 말라는 지시). 이럴 때 다음 폴백인
      // cleaned_text로 넘어가면, 정리는 됐지만 "상품 하나로 좁히기"는 안 된
      // 텍스트(사진에 같이 찍힌 다른 물건 글자까지 포함될 수 있음)가 그대로
      // 검색어가 되어버린다(사용자 리포트, 2026-08-18: 캔 사진에 옆에 있던
      // 노트북 스티커·젤리 봉지 글자까지 검색어에 섞여 나옴). LLM의 "모르겠다"
      // 판단을 존중해 지저분한 텍스트로 검색하는 대신 정직하게 다시 찍어달라고
      // 안내한다 - API 호출 자체가 실패한 경우(cleaned.error 있음)는 여기
      // 해당 안 되고 아래 폴백 체인으로 그대로 넘어간다.
      if (cleaned && !cleaned.error) {
        appendTurn(activeConversationId, {
          ...newTurn('(이미지)', ''),
          status: 'error',
          errorMessage: '사진에서 특정 상품을 찾기 어려워요. 상품만 잘 보이게 다시 찍어주시겠어요?',
        });
        return;
      }

      // 여기부터는 Groq 정제 호출 자체가 실패한 경우(cleaned가 null이거나
      // error가 있음) - cleanup.py가 재시도 후에도 실패하면 이미 로컬 규칙
      // 필터를 거친 cleaned_text를 주므로, 그마저 없을 때만 원본 ocr.text로
      // 폴백한다.
      const extractedText = (cleaned?.cleaned_text || ocr.text || '').trim();
      if (!extractedText) {
        appendTurn(activeConversationId, {
          ...newTurn('(이미지)', ''),
          status: 'error',
          errorMessage: ocr.error || '이미지에서 텍스트를 찾지 못했습니다.',
        });
        return;
      }
      await sendMessage(extractedText);
    } catch (err) {
      appendTurn(activeConversationId, {
        ...newTurn('(이미지)', ''),
        status: 'error',
        errorMessage:
          err instanceof ApiError ? err.message : '이미지 분석 중 문제가 발생했습니다. 잠시 후 다시 시도해주세요.',
      });
    } finally {
      setOcrBusy(false);
    }
  };

  // "새 검색"(사이드바) - ChatGPT의 "+New chat"과 동일하게, 지금 대화는 목록에
  // 그대로 남겨두고 활성 대화만 비운다. 다음 sendMessage가 activeConversationId
  // 없음을 보고 자동으로 새 대화를 연다(appendTurn 참고) - 여기서 바로 새
  // Conversation을 만들지 않는 이유는, 사용자가 "새 검색"만 누르고 아무것도
  // 입력하지 않으면 빈 대화가 목록에 쌓이는 걸 막기 위해서다.
  const handleReset = () => {
    setActiveConversationId(null);
  };

  const loadFromHistory = (entry: HistoryEntry) => {
    const turn: ChatTurn = {
      ...newTurn(entry.query, entry.query),
      status: 'result',
      result: entry.result,
      createdAt: entry.timestamp,
    };
    const id = crypto.randomUUID();
    setConversations((prev) => [{ id, title: entry.query, turns: [turn], updatedAt: Date.now() }, ...prev]);
    setActiveConversationId(id);
  };

  const deleteFromHistory = (id: string) => {
    if (user) {
      const token = getStoredToken();
      if (token) deleteServerHistoryEntry(token, id).catch(() => {});
      setHistory((prev) => prev.filter((h) => h.id !== id));
      return;
    }
    setHistory(deleteHistoryEntry(id));
  };

  const clearAllHistory = () => {
    if (user) {
      const token = getStoredToken();
      if (token) clearServerHistory(token).catch(() => {});
      setHistory([]);
      return;
    }
    setHistory(clearHistory());
  };

  const isBusy = ocrBusy || turns.some((t) => t.status === 'loading');

  return (
    <SearchContext.Provider
      value={{
        turns,
        isBusy,
        ocrBusy,
        history,
        sessionPreferences,
        sendMessage,
        selectFacets,
        searchBroadly,
        retryTurn,
        editTurn,
        handleImageUpload,
        handleReset,
        loadFromHistory,
        deleteFromHistory,
        clearAllHistory,
      }}
    >
      {children}
    </SearchContext.Provider>
  );
};

export const useSearch = () => {
  const ctx = useContext(SearchContext);
  if (!ctx) throw new Error('useSearch는 SearchProvider 내부에서만 사용할 수 있습니다.');
  return ctx;
};
