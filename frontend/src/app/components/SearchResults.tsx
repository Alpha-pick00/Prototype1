import { useEffect, useRef, useState } from 'react';
import { motion } from 'motion/react';
import { AlertTriangle, ArrowUpRight, Check, ImageOff, Loader2, RotateCcw, Search, Sparkles, Truck, X } from 'lucide-react';
import type {
  ClarifyFacet as ClarifyFacetType,
  DecideResult,
  DecideStage,
  BrandOption,
  Proposal,
} from '../lib/api';
import { checkClarifyFacets } from '../lib/api';
import { dedupeAppend } from '../context/SearchContext';

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
  // 계정/세션 페르소나에 기록할 수 없다. 다중 선택 지원(2026-08-24)으로 값
  // 하나가 아니라 배열이다 - 같은 facet에서 여러 개를 고를 수 있다(OR로 검색).
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
  // AI 상세검색: facet마다 하나씩 고른다. 예전엔 화면에 떠 있는 기준을 전부
  // 골라야만 검색이 실행됐는데(2026-08-13: "상세검색에서 고를때마다 검색하는걸로
  // 바꿧어 다시 다 고르면 검색되는걸로 바꿔"), 사용자가 원하는 값이 목록에
  // 없거나 모든 기준을 다 정하고 싶지 않을 수도 있어서(2026-08-14: "일부분만
  // 선택해도 넘어갈 수 있게") 지금은 "검색하기" 버튼을 눌러야 그 시점까지 고른
  // 값(일부만 골랐어도 그대로)으로 진행한다. facets는 mode==='clarify'일 때만
  // 존재하지만 Hooks는 조건부로 못 부르니 다른 모드에서는 빈 배열로 둔다.
  // 다중 선택 지원(2026-08-24, "하나밖에 선택을 못하는데 여러개 선택할 수
  // 있게") - facet 라벨 하나에 값 여러 개를 담을 수 있다(OR로 검색됨).
  const [selectedFacets, setSelectedFacets] = useState<Record<string, string[]>>({});
  const [facetQuery, setFacetQuery] = useState<Record<string, string>>({});
  // GPT 쇼핑의 "이거랑 비슷한거 더" 패턴 벤치마킹(2026-08-18, 사용자 요청
  // "GPT 쇼핑의 장점을 잘 접목시켜줘") - judge가 고른 최종 추천 하나만
  // 클릭 가능했는데, propose 단계에서 이미 받아온 다른 후보(proposals)도
  // 전부 실제 구매 가능한 상품(URL·가격 확보됨)이다. mode==='single'에서만
  // 쓰이지만 Hooks는 조건부로 못 부르니 다른 모드에서는 그냥 null로 둔다.
  const [selectedProposalUrl, setSelectedProposalUrl] = useState<string | null>(null);
  const facets = result.mode === 'clarify' ? result.options.facets : [];

  // 2026-08-18(사용자 요청: "AI상세검색에서 샤오미 즉 검색어에 관련된거를
  // 뜨게하라고 몇번을 말하냐") - 자유 텍스트로 타이핑한 값(예: "샤오미")은
  // 원래 clarify 응답의 facet 목록엔 없던 값이라, 다른 facet들도 그 값을
  // 반영해 좁혀줄 근거(options_by_selection)가 애초에 존재하지 않는다.
  // "검색어에 관련된 것"을 실제로 보여주려면 지어내지 않고 진짜로 그 결합
  // 검색어("핸드폰 샤오미")에 대해 다시 물어봐야 한다 - check_clarify_facets를
  // base_query 없이 호출하면(캐시 재사용 최적화를 건너뛰어) 11번가를 그
  // 결합 검색어로 실제로 다시 검색해서, 실제로 존재하는 샤오미 관련 facet
  // (기종·용량 등)을 새로 뽑아온다. liveFacets가 있으면 원래 facets 대신
  // 이걸 보여준다 - 라벨/구성이 달라질 수 있어 selectedFacets는 초기화한다.
  const [liveFacets, setLiveFacets] = useState<ClarifyFacetType[] | null>(null);
  const [liveFacetsLoading, setLiveFacetsLoading] = useState(false);
  const lastLiveFetchRef = useRef<string | null>(null);
  // 2026-08-18(사용자 요청: "결과 지금 좋은데 '샤오미' 친거 안사라지고 냅둬") -
  // liveFacets가 로드되면 그걸 촉발한 원래 facet("핸드폰 기종")이 새 facet
  // 세트(시리즈/모델/용량)엔 아예 없을 수 있어, 거기 타이핑해둔 "샤오미"를
  // 보여줄 곳 자체가 사라졌다. 그 값을 여기 별도로 붙잡아두고 계속 칩으로
  // 보여준다 - 최종 검색어에도 계속 포함시킨다(값이 실제로 사라지면 안 되니까).
  const [appliedFreeText, setAppliedFreeText] = useState<Record<string, string>>({});
  const displayFacets = liveFacets ?? facets;

  // 지금까지 고른 값들(어느 facet이든 - 브랜드로 한정 안 됨)로 아직 안 고른
  // facet들의 보이는 옵션을 즉시 좁힌다(추가 요청 없이, 사용자 요청 2026-08-13:
  // "삼성전자를 누르면은 시리즈에 삼성전자에 관한것만" -> 2026-08-14: "시리즈에
  // 초코파이 바나나를 골랏다면 용량에 없는것들은 선택할수없게" - 브랜드 전용
  // 특수 케이스였던 걸 모든 facet 쌍으로 일반화했다). 여러 facet을 골랐으면
  // 각각의 options_by_selection을 교집합으로 겹쳐 좁힌다.
  // 다중 선택(2026-08-24) - 한 facet에서 여러 값을 고를 수 있어, 다른
  // facet의 옵션을 좁힐 때도 그 값들 각각의 options_by_selection을
  // 합집합(OR)으로 겹친다 - "이 값 중 아무거나와 공존 가능한 옵션"이어야
  // "브랜드에 오리온·롯데 둘 다 고름 -> 둘 중 아무 브랜드에나 있는 용량은
  // 계속 보여야 함"이 성립한다.
  const visibleOptionsFor = (facet: ClarifyFacetType, selected: Record<string, string[]>): string[] => {
    let options = facet.options;
    for (const [otherLabel, values] of Object.entries(selected)) {
      if (otherLabel === facet.label || values.length === 0) continue;
      const filteredSets = values
        .map((v) => facet.options_by_selection?.[v])
        .filter((s): s is string[] => !!s);
      if (filteredSets.length === 0) continue;
      const union = new Set(filteredSets.flat());
      options = options.filter((o) => union.has(o));
    }
    return options;
  };

  const computeNextSelectedFacets = (
    prev: Record<string, string[]>,
    label: string,
    option: string
  ): Record<string, string[]> => {
    const current = prev[label] ?? [];
    const next: Record<string, string[]> = { ...prev };
    if (current.includes(option)) {
      const remaining = current.filter((v) => v !== option);
      if (remaining.length > 0) next[label] = remaining;
      else delete next[label];
    } else {
      next[label] = [...current, option];
    }
    // 이 선택으로 다른 facet의 보이는 옵션이 바뀌어 기존 선택 중 일부(또는
    // 전부)가 더 이상 유효한 값이 아니게 됐으면 지운다 - 안 그러면 서로 안
    // 맞는 조합(예: "초코파이 바나나" + "336g")이 그대로 남아있을 수 있다.
    for (const other of displayFacets) {
      if (other.label === label) continue;
      const selectedForOther = next[other.label];
      if (!selectedForOther || selectedForOther.length === 0) continue;
      const visible = visibleOptionsFor(other, next);
      const stillValid = selectedForOther.filter((v) => visible.includes(v));
      if (stillValid.length === 0) delete next[other.label];
      else if (stillValid.length !== selectedForOther.length) next[other.label] = stillValid;
    }
    return next;
  };

  // 2026-08-18 - 옵션 클릭마다 즉시 검색을 쏘면(직전 시도) 시리즈+용량+구매유형처럼
  // 여러 축을 조합해서 한 번에 검색하는 AI 상세검색 본연의 기능이 깨진다(첫 클릭에서
  // 바로 새 턴으로 넘어가버려 두 번째 축을 고를 기회가 없어짐, 사용자 리포트: "AI
  // 상세검색별로 검색할수있는 기능을 왜 없애 - 다시 살려내고") - 되돌린다. 옵션
  // 클릭은 다시 선택 상태만 바꾸고, 여러 축을 다 고른 뒤 "검색하기"를 눌러야
  // 진행된다(2026-08-13/14 결정 그대로).
  const selectFacetOption = (label: string, option: string) => {
    setSelectedFacets((prev) => computeNextSelectedFacets(prev, label, option));
  };

  // 2026-08-18(사용자 리포트: "'마우스' 검색 후 브랜드에 '삼성' 넣었는데 그
  // '삼성'(으)로 검색 버튼을 안 눌러도 검색하기가 활성화돼야지" + "특징에
  // 인체공학을 누르고 검색하기를 누르니까 삼성 키워드 자체가 사라졌어") - 두
  // 문제가 사실 하나다: 목록에 없는 값을 타이핑만 하고 그 facet의 "선택"
  // 버튼을 안 눌렀으면 selectedFacets엔 전혀 안 남아서, (1) 검색하기가
  // 안 켜지고 (2) 그 상태로 다른 facet을 확정해 검색하면 타이핑해둔 값이
  // 통째로 사라졌다. 아직 명시적으로 선택 안 됐지만(=selectedFacets에 없지만)
  // 매칭되는 옵션도 없는 타이핑 값은 "선택하겠다는 의도"로 보고, 검색하기의
  // 활성화·최종 전송 값 둘 다에 자동으로 포함시킨다(각 facet당 한 번씩 클릭을
  // 더 요구하지 않는다). 실제로 옵션 버튼을 클릭해 selectedFacets에 명시적으로
  // 들어간 값이 있으면 그게 우선한다.
  const pendingFreeTextFacets: Record<string, string> = {};
  for (const facet of displayFacets) {
    if (selectedFacets[facet.label]?.length) continue;
    const typed = (facetQuery[facet.label] ?? '').trim();
    if (!typed) continue;
    const opts = visibleOptionsFor(facet, selectedFacets);
    const hasMatch = opts.some((o) => o.toLowerCase().includes(typed.toLowerCase()));
    if (!hasMatch) pendingFreeTextFacets[facet.label] = typed;
  }
  // 타이핑한 자유 텍스트 값은 그 facet에 이미 버튼으로 고른 값이 있어도
  // 대체하지 않고 추가한다(다중 선택, 2026-08-24) - 버튼 선택과 타이핑을
  // 섞어 쓸 수 있어야 한다.
  const effectiveSelectedFacets: Record<string, string[]> = { ...selectedFacets };
  for (const [label, value] of Object.entries({ ...pendingFreeTextFacets, ...appliedFreeText })) {
    const existing = effectiveSelectedFacets[label] ?? [];
    if (!existing.includes(value)) effectiveSelectedFacets[label] = [...existing, value];
  }

  useEffect(() => {
    if (result.mode !== 'clarify') return;
    if (Object.keys(pendingFreeTextFacets).length === 0) return;
    const combined = Object.values(effectiveSelectedFacets)
      .flat()
      .reduce((acc, v) => dedupeAppend(acc, v), result.query)
      .trim();
    if (!combined || combined === lastLiveFetchRef.current) return;

    const timer = setTimeout(() => {
      lastLiveFetchRef.current = combined;
      setLiveFacetsLoading(true);
      // base_query를 안 넘긴다 - 넘기면 백엔드가 캐시 재사용을 위해 원래
      // 검색어("핸드폰")로만 검색하고 결과를 로컬 필터링하는데, 그 원래
      // 검색결과엔애초에 샤오미가 없어 필터링하면 0건이 된다. base_query
      // 없이 결합 검색어 그대로 새로 검색해야 진짜 샤오미 관련 결과가 나온다.
      checkClarifyFacets(combined)
        .then((resp) => {
          if (resp.options.facets.length > 0) {
            setLiveFacets(resp.options.facets);
            // 이 요청을 쏘게 만든 자유 텍스트 값을 계속 붙잡아둔다(예:
            // "핸드폰 기종"="샤오미") - 새로 받은 facet 세트엔 그 라벨이
            // 아예 없을 수 있어도, 사용자가 타이핑한 값 자체는 화면에서
            // 사라지면 안 되고 최종 검색어에도 계속 포함돼야 한다.
            setAppliedFreeText((prev) => ({ ...prev, ...pendingFreeTextFacets }));
            setSelectedFacets({});
          }
        })
        .catch(() => {})
        .finally(() => setLiveFacetsLoading(false));
    }, 600);

    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(pendingFreeTextFacets), JSON.stringify(effectiveSelectedFacets), result.mode, result.query]);

  if (result.mode === 'clarify') {
    const hasAnyOptions = displayFacets.length > 0;

    return (
      <Card>
        <span className="text-xs font-mono uppercase tracking-widest text-neutral-400 block mb-4">
          {hasAnyOptions ? 'AI 상세검색 · 조건을 선택하거나 채팅으로 답해주세요' : '조건을 좁힐 수 없었어요'}
        </span>
        {Object.keys(appliedFreeText).length > 0 && (
          <div className="flex flex-wrap gap-2 mb-4 last:mb-0">
            {Object.entries(appliedFreeText).map(([label, value]) => (
              <span
                key={label}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-full bg-neutral-950 text-white text-sm font-light"
              >
                {value}
                <button
                  type="button"
                  onClick={() =>
                    setAppliedFreeText((prev) => {
                      const next = { ...prev };
                      delete next[label];
                      return next;
                    })
                  }
                  aria-label={`${value} 조건 해제`}
                  className="hover:opacity-70 transition-opacity"
                >
                  <X className="w-3.5 h-3.5" />
                </button>
              </span>
            ))}
          </div>
        )}
        {liveFacetsLoading && (
          <div className="mb-4 flex items-center gap-2 text-xs font-light text-neutral-400">
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            타이핑한 검색어와 관련된 조건을 다시 찾는 중...
          </div>
        )}
        {displayFacets.map((facet) => {
          const baseOptions = visibleOptionsFor(facet, effectiveSelectedFacets);
          const query = facetQuery[facet.label] ?? '';
          const visibleOptions = query.trim()
            ? baseOptions.filter((o) => o.toLowerCase().includes(query.trim().toLowerCase()))
            : baseOptions;
          return (
            <div key={facet.label} className="mb-4 last:mb-0">
              <span className="text-xs font-light text-neutral-400 block mb-2">{facet.label}</span>
              {/* 원래 옵션이 4개 이하면 검색창 자체가 없었는데, 실제 궁합
                  데이터로 다른 facet이 이 facet의 옵션을 전부 걸러낼 수도
                  있어(정상 케이스, 예: 이미 고른 값과 공존하는 옵션이 없음) -
                  그 경우에도 직접 타이핑할 길이 있어야 막다른 길이 안 된다. */}
              {(facet.options.length > 4 || baseOptions.length === 0) && (
                <div className="relative mb-2">
                  <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-neutral-300" />
                  <input
                    type="text"
                    value={query}
                    onChange={(e) => setFacetQuery((prev) => ({ ...prev, [facet.label]: e.target.value }))}
                    onKeyDown={(e) => {
                      // 2026-08-18(사용자 리포트: "샤오미를 치고 바로 엔터하면
                      // 검색하게 바꿔줘") - 매칭 옵션이 없어 pendingFreeTextFacets에
                      // 이미 잡힌 타이핑 값(및 다른 facet에서 이미 확정된 값)을
                      // 그대로 검색하기와 동일하게 제출한다.
                      if (e.key === 'Enter' && Object.keys(effectiveSelectedFacets).length > 0) {
                        e.preventDefault();
                        onConfirmFacets(effectiveSelectedFacets);
                      }
                    }}
                    placeholder={`${facet.label} 찾기`}
                    autoComplete="off"
                    className="w-full pl-8 pr-3 py-2 rounded-full border border-black/10 text-sm font-light outline-none focus:border-neutral-950 transition-colors"
                  />
                </div>
              )}
              <div className="flex flex-wrap gap-2">
                {visibleOptions.length > 0 ? (
                  visibleOptions.map((option) => {
                    const isSelected = selectedFacets[facet.label]?.includes(option) ?? false;
                    // 사용자 페르소나(2026-08-15) - 이번 세션에서 이 라벨에 이미
                    // 골랐던 값이면 별 표시로 "평소 선택"임을 알려준다. 옵션
                    // 순서 자체(맨 앞으로 당기기)는 백엔드가 이미 반영했으니
                    // 여기서는 순수 시각적 확인 표시.
                    const isPersonaPick = !isSelected && sessionPreferences[facet.label] === option;
                    return (
                      <button
                        key={option}
                        onClick={() => selectFacetOption(facet.label, option)}
                        className={`inline-flex items-center gap-1.5 px-4 py-2 rounded-full border text-sm font-light transition-all ${
                          isSelected
                            ? 'bg-neutral-950 text-white border-neutral-950'
                            : isPersonaPick
                            ? 'border-[#4ADE80]/50 bg-[#4ADE80]/10 hover:bg-neutral-950 hover:text-white hover:border-neutral-950'
                            : 'border-black/10 hover:bg-neutral-950 hover:text-white hover:border-neutral-950'
                        }`}
                      >
                        {isPersonaPick && <Sparkles className="w-3 h-3 text-[#166534]" strokeWidth={2.5} />}
                        {option}
                      </button>
                    );
                  })
                ) : query.trim() ? (
                  // 2026-08-18(사용자 리포트: "11번가에는 아이폰 15랑 샤오미가
                  // 있어" - 목록에 없는 값을 찾으면 막다른 "일치하는 항목이
                  // 없어요"만 뜨고 검색할 방법이 없었다) - 백엔드가 미리 뽑아준
                  // 옵션 목록은 그 순간 11번가 검색 결과 상위 몇 건에서 나온
                  // 값일 뿐 전체 카탈로그가 아니다. 그 목록에 없다고 검색 자체를
                  // 막지 말고, 타이핑한 값을 그대로 이 facet의 선택값으로 써서
                  // 검색하게 한다.
                  <button
                    onClick={() => selectFacetOption(facet.label, query.trim())}
                    className="inline-flex items-center gap-1.5 px-4 py-2 rounded-full border border-dashed border-black/20 text-sm font-light text-neutral-600 hover:bg-neutral-950 hover:text-white hover:border-neutral-950 transition-all"
                  >
                    "{query.trim()}"(으)로 검색
                  </button>
                ) : (
                  <span className="text-xs font-light text-neutral-400">일치하는 항목이 없어요</span>
                )}
              </div>
            </div>
          );
        })}
        {displayFacets.length > 0 && (
          <div className="mb-4 last:mb-0 flex justify-end items-center gap-3">
            {/* 조건 없이 그냥 검색(2026-08-24) - 축을 하나도 안 골라도 원래
                질의 그대로 포괄적으로 검색할 수 있어야 한다. "검색하기"와
                달리 선택 여부와 무관하게 항상 눌린다. */}
            <button
              type="button"
              onClick={onSearchBroadly}
              className="text-sm font-light text-neutral-400 hover:text-neutral-950 transition-colors"
            >
              조건 없이 검색
            </button>
            <button
              onClick={() => onConfirmFacets(effectiveSelectedFacets)}
              disabled={Object.keys(effectiveSelectedFacets).length === 0}
              className="px-5 py-2.5 rounded-full bg-[#4ADE80] text-neutral-950 text-sm font-medium disabled:opacity-30 disabled:cursor-not-allowed hover:bg-[#3EBD6E] transition-all"
            >
              검색하기
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
