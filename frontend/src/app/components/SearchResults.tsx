import { useState } from 'react';
import { motion } from 'motion/react';
import { AlertTriangle, ArrowUpRight, Check, ImageOff, RotateCcw, Search, Sparkles, Truck } from 'lucide-react';
import type {
  ClarifyFacet as ClarifyFacetType,
  DecideResult,
  DecideStage,
  BrandOption,
  Proposal,
} from '../lib/api';

const fadeUp = {
  initial: { opacity: 0, y: 16 },
  animate: { opacity: 1, y: 0 },
  transition: { duration: 0.5, ease: [0.16, 1, 0.3, 1] as const },
};

// agent="gpt" 슬롯은 내부 식별자만 그대로고 실제 모델은 Qwen이다(2026-08-15,
// "GPT 토큰이 더 이상 없어서 Qwen 성능 제일 좋은 걸로 바꿔줘" - 백엔드
// agents/gpt.py 참고). 사용자에게 보이는 이름만 여기서 바꾼다. "gemini" 슬롯은
// 2026-08-18("Gemini 이제 안쓰니까 이름 제대로 바꿔서 코드 반영해") 식별자
// 자체를 "groq"로 리네임했다(agents/groq.py 참고) - 표시 이름도 함께 바꿨다.
const AGENT_LABEL: Record<string, string> = {
  gpt: 'Qwen',
  groq: 'Groq',
  deepseek: 'DeepSeek',
  danawa: '다나와',
  elevenst: '11번가',
};

const Card = ({ children }: { children: React.ReactNode }) => (
  <motion.div
    {...fadeUp}
    className="w-full rounded-3xl border border-black/10 bg-white/80 backdrop-blur-md shadow-[0_8px_30px_rgba(0,0,0,0.06)] p-6 md:p-8 text-left"
  >
    {children}
  </motion.div>
);

const ResetLink = ({ onReset, label = '다시 검색' }: { onReset: () => void; label?: string }) => (
  <button
    type="button"
    onClick={onReset}
    className="mt-6 inline-flex items-center gap-2 text-xs font-mono uppercase tracking-widest text-neutral-400 hover:text-neutral-950 transition-colors"
  >
    <RotateCcw className="w-3.5 h-3.5" />
    {label}
  </button>
);

// 브랜드 단축 검색/대량구매 응답을 만드는 경로(run_brand_price, "bulk" 질의)는
// 더 이상 새로 만들어지지 않지만(check_clarify_facets가 brands를 채우지 않음 -
// 일반 facet 시스템으로 대체됨), 과거 저장된 히스토리(HistoryEntry)에는 이
// 모드로 저장된 실제 기록이 있을 수 있어 그 표시 경로는 남겨둔다.
const BrandOptionRow = ({ option }: { option: BrandOption }) => (
  <a
    href={option.url}
    target="_blank"
    rel="noopener noreferrer"
    className="group flex items-center justify-between gap-4 py-4 border-b border-black/5 last:border-b-0 hover:bg-black/[0.02] transition-colors -mx-2 px-2 rounded-lg"
  >
    <div className="min-w-0">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-neutral-950">{option.brand}</span>
        {option.delivery_note && (
          <span className="inline-flex items-center gap-1 text-[11px] text-neutral-500">
            <Truck className="w-3 h-3" />
            {option.delivery_note}
          </span>
        )}
      </div>
      <p className="text-sm font-light text-neutral-500 truncate">{option.product_name}</p>
    </div>
    <div className="shrink-0 flex items-center gap-2">
      <span className="text-base font-medium text-neutral-950 whitespace-nowrap">
        {option.price || '가격 미확인'}
      </span>
      <ArrowUpRight className="w-4 h-4 text-neutral-300 group-hover:text-neutral-950 transition-colors" />
    </div>
  </a>
);

const STAGE_LABEL: Record<DecideStage, string> = {
  searching: '11번가에서 검색하고 있습니다',
};

const ProposedByChips = ({ proposedBy }: { proposedBy: string[] | null | undefined }) =>
  proposedBy && proposedBy.length > 0 ? (
    <span className="text-[10px] font-mono uppercase tracking-widest text-neutral-400">
      {proposedBy.map((a) => AGENT_LABEL[a] || a).join(' · ')}
    </span>
  ) : null;

const VerifiedBadge = ({ verified }: { verified: boolean | null | undefined }) => {
  if (verified === true) {
    return (
      <div className="shrink-0 w-5 h-5 rounded-full bg-[#4ADE80]/15 flex items-center justify-center">
        <Check className="w-3 h-3 text-[#166534]" strokeWidth={3} />
      </div>
    );
  }
  if (verified === false) {
    return (
      <div className="shrink-0 w-5 h-5 rounded-full bg-amber-500/15 flex items-center justify-center">
        <AlertTriangle className="w-3 h-3 text-amber-700" strokeWidth={2.5} />
      </div>
    );
  }
  return <div className="shrink-0 w-5 h-5 rounded-full bg-black/5" />;
};

// GPT 쇼핑식 카드(2026-08-24) - 11번가 ProductImage300을 그대로 hotlink한다
// (백엔드가 별도로 다운로드/재호스팅 안 함, backend/fetchers/elevenst.py 참고).
// 없거나(image_url null) 깨진 이미지(onError)는 같은 자리에 중립
// 플레이스홀더로 대체해 레이아웃이 흔들리지 않게 한다.
// rank(2026-08-24, 사용자 요청: "메인으로 추천해준 거랑 아래 후보로 뜨는
// 애들 번호가 있어서 구별하기 쉬웠으면 좋겠어") - 썸네일 좌상단에 순위
// 배지를 겹쳐 그린다. 메인 추천은 1, "다른 후보"는 관련도순 그대로
// 2부터 이어서 매긴다 - 사용자가 여러 카드 사이를 오갈 때 지금 보는 게
// 몇 번째 후보인지 한눈에 구별하기 위함.
const ProductThumbnail = ({
  src,
  alt,
  size = 'md',
  rank,
}: {
  src?: string | null;
  alt: string;
  size?: 'sm' | 'md';
  rank?: number;
}) => {
  const [errored, setErrored] = useState(false);
  // 2026-08-24 사용자 요청("사진 크기만 더 크게") - md 96px -> 128px,
  // sm 64px -> 96px.
  const dim = size === 'sm' ? 'w-24 h-24' : 'w-32 h-32';
  const badgeDim = size === 'sm' ? 'w-6 h-6 text-xs' : 'w-7 h-7 text-sm';
  const iconDim = size === 'sm' ? 'w-5 h-5' : 'w-7 h-7';

  const rankBadge =
    rank != null ? (
      <span
        className={`absolute -top-1.5 -left-1.5 flex items-center justify-center rounded-full font-medium ${
          rank === 1 ? 'bg-neutral-950 text-white' : 'bg-white text-neutral-600 border border-black/10'
        } ${badgeDim} shadow-sm`}
      >
        {rank}
      </span>
    ) : null;

  if (!src || errored) {
    return (
      <div className={`relative shrink-0 ${dim} rounded-lg bg-black/5 flex items-center justify-center`}>
        <ImageOff className={`${iconDim} text-neutral-300`} />
        {rankBadge}
      </div>
    );
  }

  return (
    <div className={`relative shrink-0 ${dim}`}>
      <img
        src={src}
        alt={alt}
        loading="lazy"
        onError={() => setErrored(true)}
        className={`w-full h-full rounded-lg object-cover border border-black/5 bg-white`}
      />
      {rankBadge}
    </div>
  );
};

const CandidateProgressRow = ({ proposal }: { proposal: Proposal }) => (
  <div className="flex items-start gap-3 py-2.5 border-b border-black/5 last:border-b-0">
    <VerifiedBadge verified={proposal.verified} />
    <div className="min-w-0 flex-1">
      <div className="flex items-center gap-2">
        <p className="min-w-0 flex-1 text-sm font-light text-neutral-600 truncate">
          {proposal.error ? proposal.error : `${proposal.product_name} · ${proposal.price || '가격 미확인'}`}
        </p>
        <ProposedByChips proposedBy={proposal.proposed_by} />
      </div>
      {proposal.challenge_note && (
        <p className="mt-0.5 text-xs font-light text-neutral-400 truncate">{proposal.challenge_note}</p>
      )}
    </div>
  </div>
);

// 11번가 검색 진행 상태를 턴 안에서 보여준다 - Hero.tsx가
// turn.streamingStage/streamingProposals(SearchContext.runTurn이 decideStream
// 이벤트로 채운다)를 이 컴포넌트에 그대로 넘긴다.
export const StreamingCard = ({ stage, proposals }: { stage: DecideStage; proposals: Proposal[] }) => (
  <Card>
    <div className="flex flex-col items-center text-center py-2 gap-6">
      <motion.div
        animate={{ rotate: 360 }}
        transition={{ duration: 1.2, repeat: Infinity, ease: 'linear' }}
        className="w-8 h-8 rounded-full border-2 border-black/10 border-t-[#4ADE80]"
      />
      <p className="text-sm font-light text-neutral-500">{STAGE_LABEL[stage]}</p>

      {proposals.length > 0 && (
        <div className="w-full text-left">
          {proposals.map((p, i) => (
            <CandidateProgressRow key={p.url ?? i} proposal={p} />
          ))}
        </div>
      )}
    </div>
  </Card>
);

export const ErrorCard = ({
  message,
  onReset,
  resetLabel = '다시 검색',
}: {
  message: string;
  onReset?: () => void;
  resetLabel?: string;
}) => (
  <Card>
    <div className="text-center py-4">
      <p className="text-sm font-light text-neutral-600">{message}</p>
      {onReset && <ResetLink onReset={onReset} label={resetLabel} />}
    </div>
  </Card>
);

interface Props {
  result: DecideResult;
  // 사용자 페르소나(2026-08-15) - 이번 세션에서 이미 고른 {facet 라벨: 값}.
  // 옵션 순서 자체는 백엔드가 이미 반영해 보내주므로, 여기서는 일치하는
  // 버튼에 "선호" 표시만 붙이는 시각적 용도로 쓴다.
  sessionPreferences?: Record<string, string>;
  // (사용자 페르소나, 2026-08-15) label -> 선택값 맵을 그대로 넘긴다 - 값
  // 배열만 받으면 SearchContext가 어느 facet 라벨에서 이 값을 골랐는지 몰라
  // 계정/세션 페르소나에 기록할 수 없다.
  onConfirmFacets: (selected: Record<string, string[]>) => void;
  // 조건을 하나도 안 고르고 원래 질의 그대로 포괄적으로 검색한다(2026-08-24,
  // "선택 안해도 그냥 바로 포괄적으로도 검색 가능하게 하고 싶어").
  onSearchBroadly: () => void;
}

export const SearchResults = ({
  result,
  sessionPreferences = {},
  onConfirmFacets,
  onSearchBroadly,
}: Props) => {
  // 단계별(아코디언) AI 상세검색(2026-08-27~28, 사용자 요청 - "하나의 박스를
  // 선택하면 그 아래로 계속 축소되는 형식", "재검색을 하는거 자체가 fallback
  // 구조잖아 HITL이 구현된게 아니라") - 처음 clarify 응답이 필요한 축 전체
  // (예: 시리즈→용량→색상→구매유형)를 이미 한 번에 담고 있다
  // (app/debate.py::check_clarify_facets가 _extract_facets로 표본 90개에서
  // 모든 축을 미리 추출해두고, 각 축의 options_by_selection에 다른 축과의
  // 실제 조합 가능 여부까지 계산해서 준다). 예전엔 축 하나를 답할 때마다
  // 백엔드에 facet_answers로 재검색을 걸어 "다음 축이 있는지"를 매번 다시
  // 물어봤는데, 이게 사용자 지적대로 "일단 검색해보고 안 맞으면 되돌아가는"
  // fallback처럼 동작했다(마지막 축을 답한 뒤에도 재검색 한 번을 더 기다려야
  // 최종 결과가 나와 체감 지연도 있었다). 처음 받은 축 구성을 고정된 트리로
  // 신뢰하고, 그 이후로는 순수 로컬 연산(교집합)만으로 다음 축을 좁혀
  // 보여준다 - 서버 왕복이 전혀 없다.
  const allFacets = result.mode === 'clarify' ? result.options.facets : [];

  // stepAnswers: 지금까지 확정한 축 {라벨: 값} - 순서 보존을 위해 배열도 같이 든다.
  const [answeredLabels, setAnsweredLabels] = useState<string[]>([]);
  const [stepAnswers, setStepAnswers] = useState<Record<string, string[]>>({});
  const [facetQuery, setFacetQuery] = useState('');

  // GPT 쇼핑의 "이거랑 비슷한거 더" 패턴 벤치마킹(2026-08-18, 사용자 요청
  // "GPT 쇼핑의 장점을 잘 접목시켜줘") - judge가 고른 최종 추천 하나만
  // 클릭 가능했는데, propose 단계에서 이미 받아온 다른 후보(proposals)도
  // 전부 실제 구매 가능한 상품(URL·가격 확보됨)이다. mode==='single'에서만
  // 쓰이지만 Hooks는 조건부로 못 부르니 다른 모드에서는 그냥 null로 둔다.
  const [selectedProposalUrl, setSelectedProposalUrl] = useState<string | null>(null);

  // 이미 확정된 축들의 options_by_selection을 교집합으로 겹쳐, 아직 안 답한
  // facet의 실제로 보여줄 옵션만 로컬에서 계산한다(SearchContext의 옛
  // visibleOptionsFor와 같은 원리) - 서버 재검색 없이도 "이미 고른 값과 실제로
  // 공존 가능한 옵션만" 보여줄 수 있다(1단계에서 이미 전체 조합 가능성을
  // 계산해 받아왔으므로). answers를 인자로 받는다(2026-08-28 수정) - state의
  // stepAnswers를 클로저로 직접 참조하면 advanceStep 안에서 "방금 이번
  // 선택까지 반영한 다음 축이 있는지" 계산할 때 아직 리렌더 전이라 오래된
  // stepAnswers를 보게 된다(setState는 비동기) - 호출부가 최신 answers를
  // 명시적으로 넘긴다.
  const visibleOptionsFor = (facet: ClarifyFacetType, answers: Record<string, string[]>): string[] => {
    let options = facet.options;
    for (const [label, values] of Object.entries(answers)) {
      if (label === facet.label) continue;
      const filteredSets = values
        .map((v) => facet.options_by_selection?.[v])
        .filter((s): s is string[] => !!s);
      if (filteredSets.length === 0) continue;
      const union = new Set(filteredSets.flat());
      options = options.filter((o) => union.has(o));
    }
    return options;
  };

  // 아직 안 답한 축들 중 첫 번째만 현재 단계로 보여준다 - 옵션이 0개로
  // 좁혀진 축(이미 고른 값과 공존하는 옵션이 없음)은 물어볼 이유가 없어
  // 자동으로 건너뛴다.
  const currentFacet =
    allFacets.filter((f) => !answeredLabels.includes(f.label) && visibleOptionsFor(f, stepAnswers).length > 0)[0] ??
    null;

  // 이 축을 답하면 로컬에서만 진행한다(서버 호출 없음) - 다음 축이 남아있으면
  // 바로 그 축을 보여주고, 다 답했으면(remaining이 비면) 그 즉시 최종 검색으로
  // 넘어간다. "재검색으로 다음 축이 있는지 확인"하는 단계 자체가 없어져
  // 마지막 축을 고른 즉시 검색이 실행된다.
  const advanceStep = (label: string, value: string) => {
    if (result.mode !== 'clarify') return;
    const nextAnsweredLabels = [...answeredLabels, label];
    const nextAnswers: Record<string, string[]> = { ...stepAnswers, [label]: [value] };
    const hasMoreSteps = allFacets.some(
      (f) => !nextAnsweredLabels.includes(f.label) && visibleOptionsFor(f, nextAnswers).length > 0
    );
    setAnsweredLabels(nextAnsweredLabels);
    setStepAnswers(nextAnswers);
    setFacetQuery('');
    if (!hasMoreSteps) {
      onConfirmFacets(nextAnswers);
    }
  };

  // 확정한 축을 되돌아가 다시 고른다 - 그 축 이후에 답한 값은 전제가
  // 깨지므로(예: 기종을 바꾸면 그 아래서 고른 용량이 안 맞을 수 있음) 같이 지운다.
  const rewindTo = (index: number) => {
    const trimmedLabels = answeredLabels.slice(0, index);
    const trimmedAnswers: Record<string, string[]> = {};
    for (const label of trimmedLabels) trimmedAnswers[label] = stepAnswers[label];
    setAnsweredLabels(trimmedLabels);
    setStepAnswers(trimmedAnswers);
    setFacetQuery('');
  };

  if (result.mode === 'clarify') {
    const hasCurrentStep = !!currentFacet;
    const query = facetQuery.trim();
    const baseOptions = currentFacet ? visibleOptionsFor(currentFacet, stepAnswers) : [];
    const visibleOptions = query
      ? baseOptions.filter((o) => o.toLowerCase().includes(query.toLowerCase()))
      : baseOptions;

    return (
      <Card>
        <span className="text-xs font-mono uppercase tracking-widest text-neutral-400 block mb-4">
          {hasCurrentStep
            ? 'AI 상세검색 · 조건을 하나씩 선택해주세요'
            : answeredLabels.length > 0
            ? 'AI 상세검색 · 조건에 맞는 상품을 검색하고 있습니다'
            : '조건을 좁힐 수 없었어요'}
        </span>
        {/* 이미 확정한 축들 - 접힌 요약 칩. 클릭하면 그 축으로 되돌아간다. */}
        {answeredLabels.length > 0 && (
          <div className="flex flex-wrap gap-2 mb-5 last:mb-0">
            {answeredLabels.map((label, index) => (
              <button
                key={label}
                type="button"
                onClick={() => rewindTo(index)}
                className="group inline-flex items-center gap-1.5 px-4 py-2 rounded-full bg-neutral-950 text-white text-sm font-light hover:bg-neutral-800 transition-colors"
              >
                <Check className="w-3.5 h-3.5 text-[#4ADE80]" strokeWidth={3} />
                <span className="text-neutral-400 font-mono text-[10px] uppercase tracking-widest">{label}</span>
                {stepAnswers[label]?.[0]}
              </button>
            ))}
          </div>
        )}
        {currentFacet && (
          <div className="mb-4 last:mb-0">
            <span className="text-xs font-light text-neutral-400 block mb-2">{currentFacet.label}</span>
            {currentFacet.options.length > 4 && (
              <div className="relative mb-2">
                <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-neutral-300" />
                <input
                  type="text"
                  value={facetQuery}
                  onChange={(e) => setFacetQuery(e.target.value)}
                  onKeyDown={(e) => {
                    // 2026-08-18(사용자 리포트: "샤오미를 치고 바로 엔터하면
                    // 검색하게 바꿔줘") - 목록에 없는 값을 타이핑하고 바로
                    // 제출하면 그 값을 이 축의 답으로 확정하고 다음 단계로 진행한다.
                    if (e.key === 'Enter' && query) {
                      e.preventDefault();
                      advanceStep(currentFacet.label, query);
                    }
                  }}
                  placeholder={`${currentFacet.label} 찾기`}
                  autoComplete="off"
                  className="w-full pl-8 pr-3 py-2 rounded-full border border-black/10 text-sm font-light outline-none focus:border-neutral-950 transition-colors"
                />
              </div>
            )}
            <div className="flex flex-wrap gap-2">
              {visibleOptions.length > 0 ? (
                visibleOptions.map((option) => {
                  // 사용자 페르소나(2026-08-15) - 이번 세션에서 이 라벨에 이미
                  // 골랐던 값이면 별 표시로 "평소 선택"임을 알려준다.
                  const isPersonaPick = sessionPreferences[currentFacet.label] === option;
                  return (
                    <button
                      key={option}
                      onClick={() => advanceStep(currentFacet.label, option)}
                      className={`inline-flex items-center gap-1.5 px-4 py-2 rounded-full border text-sm font-light transition-all ${
                        isPersonaPick
                          ? 'border-[#4ADE80]/50 bg-[#4ADE80]/10 hover:bg-neutral-950 hover:text-white hover:border-neutral-950'
                          : 'border-black/10 hover:bg-neutral-950 hover:text-white hover:border-neutral-950'
                      }`}
                    >
                      {isPersonaPick && <Sparkles className="w-3 h-3 text-[#166534]" strokeWidth={2.5} />}
                      {option}
                    </button>
                  );
                })
              ) : query ? (
                // 2026-08-18(사용자 리포트: "11번가에는 아이폰 15랑 샤오미가
                // 있어" - 목록에 없는 값을 찾으면 막다른 "일치하는 항목이
                // 없어요"만 뜨고 검색할 방법이 없었다) - 백엔드가 미리 뽑아준
                // 옵션 목록은 그 순간 11번가 검색 결과 상위 몇 건에서 나온
                // 값일 뿐 전체 카탈로그가 아니다. 타이핑한 값을 그대로 이
                // 축의 답으로 써서 다음 단계로 진행한다.
                <button
                  onClick={() => advanceStep(currentFacet.label, query)}
                  className="inline-flex items-center gap-1.5 px-4 py-2 rounded-full border border-dashed border-black/20 text-sm font-light text-neutral-600 hover:bg-neutral-950 hover:text-white hover:border-neutral-950 transition-all"
                >
                  "{query}"(으)로 검색
                </button>
              ) : (
                <span className="text-xs font-light text-neutral-400">일치하는 항목이 없어요</span>
              )}
            </div>
          </div>
        )}
        {(hasCurrentStep || answeredLabels.length > 0) && (
          <div className="mb-4 last:mb-0 flex justify-end items-center gap-3">
            {/* 조건 없이 그냥 검색(2026-08-24) - 남은 축을 마저 안 골라도 지금까지
                고른 값(또는 하나도 없으면 원래 질의) 그대로 검색할 수 있어야 한다. */}
            <button
              type="button"
              onClick={() => (answeredLabels.length > 0 ? onConfirmFacets(stepAnswers) : onSearchBroadly())}
              className="text-sm font-light text-neutral-400 hover:text-neutral-950 transition-colors"
            >
              {answeredLabels.length > 0 ? '여기까지만 검색' : '조건 없이 검색'}
            </button>
          </div>
        )}
      </Card>
    );
  }

  // "bulk"/"brand_price" 모드는 더 이상 새로 만들어지지 않지만(위 BrandOptionRow
  // 주석 참고), 과거 히스토리 항목을 열었을 때는 여전히 나올 수 있어 표시
  // 경로를 남겨둔다.
  if (result.mode === 'brand_price') {
    if (result.error || !result.option) {
      return <ErrorCard message={result.error || '해당 브랜드 상품을 찾지 못했습니다.'} />;
    }
    return (
      <Card>
        <span className="text-xs font-mono uppercase tracking-widest text-neutral-400 block mb-4">
          {result.brand} 최저가
        </span>
        <BrandOptionRow option={result.option} />
      </Card>
    );
  }

  if (result.mode === 'bulk') {
    const { decision, price_range } = result;
    return (
      <Card>
        <div className="flex items-baseline justify-between mb-2">
          <span className="text-xs font-mono uppercase tracking-widest text-neutral-400">
            브랜드별 최저가 {decision.options.length}개
          </span>
          {price_range && (
            <span className="text-xs font-light text-neutral-400">
              {price_range.min} ~ {price_range.max}
            </span>
          )}
        </div>
        <p className="text-sm font-light text-neutral-500 mb-4">{decision.reasoning}</p>
        <div>
          {decision.options.map((option) => (
            <BrandOptionRow key={option.brand} option={option} />
          ))}
        </div>
      </Card>
    );
  }

  // mode === 'single'
  const { decision, proposals } = result;

  // selectedProposalUrl이 가리키는 후보를 찾으면 그걸 메인 카드에 보여준다 -
  // 추가 요청 없이(proposals가 이미 URL/가격까지 다 갖고 있어서) 즉시
  // 전환된다. "AI추천" 버튼은 최종 추천/다른 후보 상관없이 항상 같은
  // 자리에 뜨고(2026-08-24, "다른 후보자랑 똑같이 넣어줘"), 최종 추천을
  // 보는 중엔 이미 selectedProposalUrl이 null이라 눌러도 상태가 안 바뀐다.
  const selectedProposal = selectedProposalUrl
    ? proposals.find((p) => p.url === selectedProposalUrl) ?? null
    : null;
  const isAlternate = selectedProposal != null;
  const displayed = selectedProposal ?? decision;
  // 관련도순으로 이미 정렬돼 있어(app.debate._rank_by_relevance) 상위 4개만
  // 잘라도 가장 관련성 높은 후보가 빠지지 않는다(2026-08-24 사용자 요청 -
  // 후보 전부를 보여주면 카드가 너무 많아 보기 불편함).
  const MAX_OTHER_PROPOSALS = 4;
  let otherProposals = proposals.filter((p) => p.url !== displayed.url).slice(0, MAX_OTHER_PROPOSALS);
  // 최종 추천(1번)은 관련도 자체는 낮을 수 있다(추천 Agent가 가격/리뷰
  // 기준으로 고르기 때문) - 다른 후보를 보는 중(isAlternate)에 1번이 상위
  // 4개 밖으로 밀려나면 "1번으로 돌아갈 번호"가 목록에서 아예 사라진다
  // (2026-08-24 버그 리포트 - "다시 1번으로 돌아갈 수 있게 번호가 떠야
  // 하는데 번호가 제대로 안뜨는 것 같다"). isAlternate일 땐 1번을 항상
  // 강제로 끼워 넣는다.
  if (isAlternate && !otherProposals.some((p) => p.url === decision.url)) {
    const decisionProposal = proposals.find((p) => p.url === decision.url);
    if (decisionProposal) {
      otherProposals = [decisionProposal, ...otherProposals.slice(0, MAX_OTHER_PROPOSALS - 1)];
    }
  }

  // 순위 배지(2026-08-24, "메인 추천이랑 다른 후보 번호로 구별하기 쉬웠으면") -
  // url에 고정된 번호를 매겨서, 다른 후보를 눌러 메인 카드에 띄워도(displayed가
  // 바뀌어도) 그 상품의 번호 자체는 안 바뀐다. 최종 추천은 항상 1, 나머지는
  // proposals의 관련도순 그대로 2부터 이어서 매긴다.
  const rankByUrl: Record<string, number> = {};
  if (decision.url) rankByUrl[decision.url] = 1;
  let nextRank = 2;
  for (const p of proposals) {
    if (p.url && !(p.url in rankByUrl)) rankByUrl[p.url] = nextRank++;
  }

  // "만족도 최고" 배지(2026-08-24, 사용자 요청 - "만족도 최고, AI가 1등으로
  // 추천 이런식으로 표기") - 리뷰 수가 최소 기준 미만인 후보는 아예 후보에서
  // 뺀다(리뷰 0건짜리가 "만족도 최고"로 뽑히는 문제 방지, 이전에 실제로
  // 겪은 버그). AI 1위 추천(decision)은 이미 자기 배지가 있으니 여기 풀에서
  // 제외해 배지 두 개가 한 카드에 겹치지 않게 한다. 기준 미달이면 아예 배지
  // 자체를 안 보여준다(억지로 승자를 만들지 않는다).
  const MIN_REVIEWS_FOR_SATISFACTION_BADGE = 5;
  const satisfactionCandidates = proposals.filter(
    (p) => p.url && p.url !== decision.url && (p.review_count ?? 0) >= MIN_REVIEWS_FOR_SATISFACTION_BADGE
  );
  const mostSatisfied = satisfactionCandidates.reduce<Proposal | null>((best, p) => {
    if (!best) return p;
    const pScore = [p.buy_satisfy ?? 0, p.review_count ?? 0];
    const bestScore = [best.buy_satisfy ?? 0, best.review_count ?? 0];
    return pScore[0] > bestScore[0] || (pScore[0] === bestScore[0] && pScore[1] > bestScore[1]) ? p : best;
  }, null);
  const mostSatisfiedUrl = mostSatisfied?.url ?? null;

  const TopBadge = ({ url }: { url: string | null | undefined }) => {
    if (!url) return null;
    if (url === decision.url) {
      return (
        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-neutral-950 text-white">
          AI 1위 추천
        </span>
      );
    }
    if (url === mostSatisfiedUrl) {
      return (
        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-[#4ADE80]/15 text-[#166534]">
          만족도 최고
        </span>
      );
    }
    return null;
  };

  return (
    <Card>
      <div className="flex items-center mb-4">
        <button
          type="button"
          // 최종 추천을 보는 중일 땐 이미 null이라 눌러도 상태 변화가 없다 -
          // 다른 후보를 볼 때와 똑같은 자리에 똑같이 "AI추천"을 보여달라는
          // 요청(2026-08-24)이라 isAlternate 여부와 무관하게 항상 렌더링한다.
          onClick={() => setSelectedProposalUrl(null)}
          className="text-xs font-mono uppercase tracking-widest text-neutral-400 hover:text-neutral-950 transition-colors"
        >
          AI추천
        </button>
      </div>
      <a
        href={displayed.url ?? undefined}
        target="_blank"
        rel="noopener noreferrer"
        className="group flex items-start justify-between gap-4 mb-6"
      >
        <div className="flex items-start gap-3 min-w-0">
          <ProductThumbnail
            src={displayed.image_url}
            alt={displayed.product_name ?? ''}
            size="md"
            rank={displayed.url ? rankByUrl[displayed.url] : undefined}
          />
          <div className="min-w-0">
            <TopBadge url={displayed.url} />
            <p className="mt-1 text-lg font-medium text-neutral-950">{displayed.product_name}</p>
            <p className="text-sm font-light text-neutral-500">{displayed.retailer}</p>
            <p className="mt-1.5 text-sm font-light text-neutral-600 leading-relaxed break-keep">{displayed.reasoning}</p>
          </div>
        </div>
        <div className="shrink-0 flex items-center gap-2">
          <span className="text-xl font-medium text-neutral-950 whitespace-nowrap">
            {displayed.price || '가격 미확인'}
          </span>
          <ArrowUpRight className="w-5 h-5 text-neutral-300 group-hover:text-neutral-950 transition-colors" />
        </div>
      </a>

      {/* 취향 주도 카테고리(패션의류/잡화 등)에서만 채워진다 - GPT 쇼핑의
          스타일 가이드 벤치마킹(2026-08-19). 그룹의 상품명/가격/판매처는
          group 자체가 아니라 반드시 실제 proposals에서 url로 찾아 쓴다 -
          백엔드가 이미 그라운딩 검증을 했지만, 프론트도 group에 직접 실린
          텍스트가 아니라 검증된 proposal 데이터를 신뢰하는 편이 안전하다. */}
      {result.style_guide && result.style_guide.groups.length > 0 ? (
        <div className="pt-4 border-t border-black/5">
          {result.style_guide.intro && (
            <p className="text-sm font-light text-neutral-600 leading-relaxed mb-4">
              {result.style_guide.intro}
            </p>
          )}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
            {result.style_guide.groups
              .filter((g) => g.url !== displayed.url)
              .map((g) => {
                const matched = proposals.find((p) => p.url === g.url);
                if (!matched) return null;
                return (
                  <button
                    key={g.url}
                    type="button"
                    // 1번(최종 추천) 자기 자신을 눌렀으면 proposals 목록의
                    // 평범한 버전이 아니라 진짜 "최종 추천" 상태(decision의
                    // 원래 reasoning·헤더 라벨)로 돌아가야 한다 - null로
                    // 리셋하면 displayed가 다시 decision을 가리킨다.
                    onClick={() => setSelectedProposalUrl(g.url === decision.url ? null : g.url)}
                    className="flex items-start gap-2 text-left rounded-lg -mx-2 px-2 py-2 hover:bg-black/[0.03] transition-colors cursor-pointer"
                  >
                    <ProductThumbnail
                      src={matched.image_url}
                      alt={matched.product_name ?? ''}
                      size="sm"
                      rank={matched.url ? rankByUrl[matched.url] : undefined}
                    />
                    <div className="min-w-0 flex flex-col items-start gap-1">
                      <TopBadge url={matched.url} />
                      <span className="text-xs font-medium text-neutral-950">{g.label}</span>
                      <span className="text-xs font-light text-neutral-500 leading-relaxed">{g.description}</span>
                      <span className="text-xs font-light text-neutral-600 mt-1">
                        {matched.product_name} · {matched.price || '가격 미확인'}
                      </span>
                    </div>
                  </button>
                );
              })}
          </div>
          {result.style_guide.closing_pick && (
            <p className="text-xs font-light text-neutral-400 leading-relaxed mt-4 pt-4 border-t border-black/5">
              {result.style_guide.closing_pick}
            </p>
          )}
        </div>
      ) : (
        otherProposals.length > 0 && (
          <div className="pt-4 border-t border-black/5">
            <span className="text-[11px] font-mono uppercase tracking-widest text-neutral-400 block mb-2">
              다른 후보
            </span>
            {/* 2026-08-24 사용자 요청 - 후보가 2개나 4개면 2열 그리드가 꽉
                차서 예쁜데, 3개면 2+1로 어중간하게 남는다. 3개일 때만 3열
                한 줄로 바꾸고, 카드도 가로형(이미지 왼쪽) 대신 세로형
                (이미지 위, 텍스트 아래)으로 바꿔 좁은 칸에서도 정사각형에
                가깝게 보이게 한다. */}
            <div className={`grid grid-cols-1 gap-4 ${otherProposals.length === 3 ? 'sm:grid-cols-3' : 'sm:grid-cols-2'}`}>
              {otherProposals.map((p, i) => {
                const isPickable = !p.error && !!p.url && !!p.product_name;
                const isSquare = otherProposals.length === 3;
                return (
                  <button
                    key={p.url ?? i}
                    type="button"
                    disabled={!isPickable}
                    // 1번(최종 추천) 자기 자신을 눌렀으면 proposals 목록의
                    // 평범한 버전이 아니라 진짜 "최종 추천" 상태(decision의
                    // 원래 reasoning·헤더 라벨)로 돌아가야 한다 - null로
                    // 리셋하면 displayed가 다시 decision을 가리킨다(2026-08-24
                    // 버그 리포트 - "다른 후보 보고 1번으로 돌아가면 요약본으로
                    // 뜬다").
                    onClick={() => isPickable && setSelectedProposalUrl(p.url === decision.url ? null : p.url)}
                    className={`flex gap-3 text-sm rounded-lg -mx-2 px-3 py-2.5 transition-colors ${
                      isSquare ? 'flex-col items-center text-center' : 'items-start text-left'
                    } ${isPickable ? 'hover:bg-black/[0.03] cursor-pointer' : 'cursor-default'}`}
                  >
                    <ProductThumbnail
                      src={p.image_url}
                      alt={p.product_name ?? ''}
                      size="sm"
                      rank={p.url ? rankByUrl[p.url] : undefined}
                    />
                    <div className={`min-w-0 flex-1 ${isSquare ? 'flex flex-col items-center' : ''}`}>
                      <TopBadge url={p.url} />
                      <ProposedByChips proposedBy={p.proposed_by} />
                      <p className={`mt-1 font-light text-neutral-600 ${isSquare ? 'line-clamp-2' : 'truncate'}`}>
                        {p.error ? p.error : `${p.product_name} · ${p.price || '가격 미확인'}`}
                      </p>
                      {!p.error && p.reasoning && (
                        <p className="mt-0.5 text-xs font-light text-neutral-400 leading-snug line-clamp-2 break-keep">
                          {p.reasoning}
                        </p>
                      )}
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        )
      )}
    </Card>
  );
};
