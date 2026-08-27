import { useRef } from 'react';
import { motion } from 'motion/react';
import { Sparkles, Search, ShieldCheck, SlidersHorizontal, Scale, CheckCircle2 } from 'lucide-react';

// 2026-08-27 전면 개편 - Google ADK 8단계 SequentialAgent(backend/app/
// adk_pipeline.py) 기준으로 다시 정리했다. 이전 버전(6단계, "추천 Agent"가
// Qwen 하나만 표시)은 그라운딩 검증(challenge, DeepSeek) 단계가 통째로 빠져
// 있었고, 등급 편중(예: "아이폰 17" 검색에 프로/프로맥스만 매핑되던 문제)을
// 잡는 보정 검색도 반영되지 않았다. 실제 라이브 경로는 8단계지만, 사용자가
// 매 검색마다 체감하는 단위로 묶어 6개 카드로 보여준다(내부 8단계 -> 사용자
// 체감 6단계 매핑: ①refine ②search+보정 ③propose+filter_merge ④challenge
// ⑤apply_challenge+judge ⑥응답). 모델 배정은 backend/app/adk_pipeline.py의
// LiteLlm 호출부 기준 - refine/표기변형/등급판정은 HCX·DeepSeek, 관련도 랭킹과
// 최종 judge는 Qwen.
const steps = [
  {
    icon: Sparkles,
    title: '질의 정제 & 가격 조건 분리',
    description: 'HCX가 대화체 문장에서 실제 찾는 상품명만 추려내고, "2만원대"처럼 가격 조건이 있으면 검색어와 분리해 따로 기억해둡니다.',
  },
  {
    icon: Search,
    title: '11번가 검색 & 표본 보정',
    description: '11번가 오픈 API로 실시간 판매 정보를 받아옵니다. 액세서리가 표본을 도배하거나 상위 등급 매물만 잡히면(예: 기본형을 찾는데 프로만 나옴) AI가 감지해 보정 검색을 한 번 더 붙입니다.',
  },
  {
    icon: ShieldCheck,
    title: '관련성 검증 & 관련도 정렬',
    description: '검색 결과 중 실제로 찾는 상품이 맞는지 상품명 유사도와 의미 유사도로 확인하고, 통과한 후보를 Qwen 임베딩으로 관련도순 정렬합니다.',
  },
  {
    icon: SlidersHorizontal,
    title: 'AI 교차 검증',
    description: '관련성 검증을 통과한 상위 후보를 DeepSeek이 한 번 더 의미 기반으로 재확인해, 표기만 비슷할 뿐 실제로는 다른 상품(액세서리·다른 모델 등)인지 걸러냅니다.',
  },
  {
    icon: Scale,
    title: '추천 Agent가 최종 판단',
    description: '검증된 후보 중 가격, 리뷰 수, 구매만족도까지 함께 보고 Qwen이 가장 추천할 만한 상품을 고릅니다. 후보가 하나뿐이면 AI 호출 없이 바로 확정합니다.',
  },
  {
    icon: CheckCircle2,
    title: '하나의 확실한 답',
    description: '최종 추천에는 상품명, 가격, 판매처와 선택 이유가 함께 따라붙고, 함께 볼만한 다른 후보도 각각의 이유와 함께 보여줍니다.',
  },
];

export const HowWeCurate = () => {
  const containerRef = useRef(null);

  return (
    <section ref={containerRef} id="how-we-curate" className="py-32 relative bg-white overflow-hidden">
      {/* Background Grid - Technical Texture, echoes About */}
      <div className="absolute inset-0 bg-[linear-gradient(to_right,#00000008_1px,transparent_1px),linear-gradient(to_bottom,#00000008_1px,transparent_1px)] bg-[size:4rem_4rem] [mask-image:radial-gradient(ellipse_60%_50%_at_50%_50%,#000_70%,transparent_100%)] pointer-events-none" />

      <div className="container mx-auto px-6 relative z-10">

        {/* Section Header */}
        <div className="mb-20 grid md:grid-cols-2 gap-16 items-end">
          <div>
            <div className="flex items-center gap-6 mb-8">
              <div className="flex items-baseline gap-3">
                <span className="font-serif italic text-lg text-neutral-950">04</span>
                <span className="text-xs font-mono uppercase tracking-[0.3em] text-neutral-600">The Process</span>
              </div>
              <div className="h-px w-32 bg-gradient-to-r from-black/30 to-transparent" />
            </div>
            <h2 className="text-5xl md:text-8xl font-medium tracking-tighter leading-[0.9]">
              How We <br />
              <span className="italic font-serif" style={{ color: '#4ADE80' }}>Curate</span>.
            </h2>
          </div>

          <motion.div
            initial={{ opacity: 0, x: 20 }}
            whileInView={{ opacity: 1, x: 0 }}
            viewport={{ once: true }}
            transition={{ delay: 0.2, duration: 0.8 }}
            className="md:pl-12 border-l border-black/10 relative"
          >
            <div className="absolute top-0 left-[-1px] h-12 w-[1px] bg-gradient-to-b from-black to-transparent" />
            <p className="text-xl md:text-2xl font-light text-neutral-700 leading-relaxed">
              추측하지 않습니다. 11번가 공식 데이터로 실제로 판매 중인 상품인지부터 확인하고, DeepSeek이
              한 번 더 의미로 재검증한 후보만 <span className="font-medium" style={{ color: '#4ADE80' }}>Qwen</span>이
              가격과 리뷰, 구매만족도까지 함께 보고 답합니다.
            </p>
          </motion.div>
        </div>

        {/* Diagram */}
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true }}
          transition={{ duration: 0.8 }}
          className="rounded-2xl bg-black/[0.02] border border-black/5 p-6 md:p-10 mb-8 overflow-x-auto"
        >
          <svg
            viewBox="0 0 1366 220"
            role="img"
            aria-label="검색어가 들어오면 HCX가 질의를 정제하면서 가격 조건을 따로 떼어낸다. 정제된 검색어로 11번가 공식 오픈 API를 조회하고, 액세서리 도배나 등급 편중이 감지되면 AI가 보정 검색을 한 번 더 붙인다. 그 결과를 상품명 유사도와 의미 유사도로 관련성 검증한 뒤 Qwen 임베딩으로 관련도순 정렬한다. 상위 후보는 DeepSeek이 한 번 더 의미 기반으로 교차 검증한다. 마지막으로 Qwen이 가격, 리뷰, 구매만족도를 함께 보고 최종 추천 하나와 다른 후보들의 이유를 만든다 - 후보가 하나뿐이면 이 단계는 AI 호출 없이 바로 확정된다."
            className="w-full h-auto min-w-[1180px]"
          >
            <defs>
              <marker id="hwc-arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill="#a3a3a3" />
              </marker>
              <marker id="hwc-arrow-accent" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
                <path d="M0,0 L10,5 L0,10 z" fill="#4ADE80" />
              </marker>
            </defs>

            {/* Query */}
            <rect x="8" y="60" width="128" height="90" rx="12" fill="#ffffff" stroke="#e5e5e5" />
            <text x="72" y="98" textAnchor="middle" fontSize="12.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">검색어 입력</text>
            <text x="72" y="118" textAnchor="middle" fontSize="9" fill="#8a8a8a" fontFamily="-apple-system, sans-serif">"아이폰 17 프로"</text>

            <line x1="136" y1="105" x2="164" y2="105" stroke="#c7c7c7" strokeWidth="1.4" markerEnd="url(#hwc-arrow)" />

            {/* 1. Refine + price split (HCX) */}
            <rect x="164" y="60" width="150" height="90" rx="12" fill="#ffffff" stroke="#e5e5e5" />
            <text x="239" y="90" textAnchor="middle" fontSize="10" fontWeight="700" fill="#a3a3a3" fontFamily="ui-monospace, monospace">01</text>
            <text x="239" y="106" textAnchor="middle" fontSize="11.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">질의 정제 ·</text>
            <text x="239" y="120" textAnchor="middle" fontSize="11.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">가격 조건 분리</text>
            <text x="239" y="136" textAnchor="middle" fontSize="9.5" fill="#8a8a8a" fontFamily="-apple-system, sans-serif">HCX</text>

            <line x1="314" y1="105" x2="342" y2="105" stroke="#c7c7c7" strokeWidth="1.4" markerEnd="url(#hwc-arrow)" />

            {/* 2. Search + correction (11st + HCX/DeepSeek) */}
            <rect x="342" y="60" width="160" height="90" rx="12" fill="#ffffff" stroke="#e5e5e5" />
            <text x="422" y="90" textAnchor="middle" fontSize="10" fontWeight="700" fill="#a3a3a3" fontFamily="ui-monospace, monospace">02</text>
            <text x="422" y="106" textAnchor="middle" fontSize="11.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">11번가 검색 ·</text>
            <text x="422" y="120" textAnchor="middle" fontSize="11.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">표본 보정</text>
            <text x="422" y="136" textAnchor="middle" fontSize="9.5" fill="#8a8a8a" fontFamily="-apple-system, sans-serif">HCX · DeepSeek</text>

            <line x1="502" y1="105" x2="530" y2="105" stroke="#c7c7c7" strokeWidth="1.4" markerEnd="url(#hwc-arrow)" />

            {/* 3. Relevance + rank (Qwen embedding) */}
            <rect x="530" y="60" width="160" height="90" rx="12" fill="#ffffff" stroke="#e5e5e5" />
            <text x="610" y="90" textAnchor="middle" fontSize="10" fontWeight="700" fill="#a3a3a3" fontFamily="ui-monospace, monospace">03</text>
            <text x="610" y="106" textAnchor="middle" fontSize="11.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">관련성 검증 ·</text>
            <text x="610" y="120" textAnchor="middle" fontSize="11.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">관련도 정렬</text>
            <text x="610" y="136" textAnchor="middle" fontSize="9.5" fill="#8a8a8a" fontFamily="-apple-system, sans-serif">규칙 · Qwen 임베딩</text>

            <line x1="690" y1="105" x2="718" y2="105" stroke="#c7c7c7" strokeWidth="1.4" markerEnd="url(#hwc-arrow)" />

            {/* 4. Challenge (DeepSeek) */}
            <rect x="718" y="60" width="150" height="90" rx="12" fill="#ffffff" stroke="#e5e5e5" />
            <text x="793" y="90" textAnchor="middle" fontSize="10" fontWeight="700" fill="#a3a3a3" fontFamily="ui-monospace, monospace">04</text>
            <text x="793" y="106" textAnchor="middle" fontSize="11.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">AI 교차 검증</text>
            <text x="793" y="122" textAnchor="middle" fontSize="9.5" fill="#8a8a8a" fontFamily="-apple-system, sans-serif">DeepSeek</text>
            <text x="793" y="136" textAnchor="middle" fontSize="9" fill="#a3a3a3" fontFamily="-apple-system, sans-serif">상위 후보 재확인</text>

            <line x1="868" y1="105" x2="896" y2="105" stroke="#c7c7c7" strokeWidth="1.4" markerEnd="url(#hwc-arrow)" />

            {/* 5. Judge (Qwen) */}
            <rect x="896" y="50" width="160" height="110" rx="14" fill="rgba(74,222,128,0.08)" stroke="#4ADE80" strokeWidth="1.6" />
            <text x="976" y="86" textAnchor="middle" fontSize="10" fontWeight="700" fill="#166534" fontFamily="ui-monospace, monospace">05</text>
            <text x="976" y="102" textAnchor="middle" fontSize="13" fontWeight="700" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">추천 Agent</text>
            <text x="976" y="118" textAnchor="middle" fontSize="10" fill="#166534" fontFamily="-apple-system, sans-serif">Qwen</text>
            <text x="976" y="132" textAnchor="middle" fontSize="9" fill="#166534" fontFamily="-apple-system, sans-serif">가격 · 리뷰 · 만족도</text>

            <line x1="1056" y1="105" x2="1084" y2="105" stroke="#4ADE80" strokeWidth="1.6" markerEnd="url(#hwc-arrow-accent)" />

            {/* 6. Final */}
            <rect x="1084" y="50" width="130" height="110" rx="14" fill="#0a0a0a" />
            <text x="1149" y="86" textAnchor="middle" fontSize="10" fontWeight="700" fill="#737373" fontFamily="ui-monospace, monospace">06</text>
            <text x="1149" y="104" textAnchor="middle" fontSize="12.5" fontWeight="600" fill="#ffffff" fontFamily="-apple-system, sans-serif">최종 추천</text>
            <text x="1149" y="122" textAnchor="middle" fontSize="9.5" fill="#c7c7c7" fontFamily="-apple-system, sans-serif">상품 · 가격</text>
            <text x="1149" y="136" textAnchor="middle" fontSize="9.5" fill="#c7c7c7" fontFamily="-apple-system, sans-serif">판매처 · 근거</text>

            <text x="8" y="196" fontSize="10.5" fill="#a3a3a3" fontFamily="ui-monospace, monospace" letterSpacing="0.02em">
              * "최저가"는 11번가 판매 목록 기준입니다(다른 쇼핑몰과의 비교는 포함하지 않습니다).
            </text>
          </svg>
        </motion.div>

        {/* Steps legend */}
        <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-x-8 gap-y-16 border-t border-black/5 pt-16">
          {steps.map((step, index) => (
            <motion.div
              key={step.title}
              initial={{ opacity: 0, y: 30 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ delay: index * 0.1, duration: 0.6 }}
            >
              <div className="flex items-baseline gap-3 mb-4">
                <span className="font-serif italic text-sm text-neutral-400">0{index + 1}</span>
                <div className="w-9 h-9 rounded-full bg-black/5 flex items-center justify-center">
                  <step.icon className="w-4 h-4 text-neutral-950" />
                </div>
              </div>
              <h3 className="text-lg font-medium tracking-tight mb-2">{step.title}</h3>
              <p className="text-sm font-light text-neutral-600 leading-relaxed">{step.description}</p>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
};
