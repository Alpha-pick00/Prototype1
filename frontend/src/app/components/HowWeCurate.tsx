import { useRef } from 'react';
import { motion } from 'motion/react';
import { Sparkles, Search, ShieldCheck, SlidersHorizontal, Scale, CheckCircle2 } from 'lucide-react';

// 2026-08-25 전면 개편 - 이 페이지는 한동안 다나와 스크래핑 + Qwen·Groq·DeepSeek
// 3개 모델이 서로 교차검증하던 구 아키텍처를 설명하고 있었다(사용자 리포트: "How
// we curate 부분 내용이 전반적으로 바뀌어서 수정해야 하는데"). 그 구조는 11번가
// 공식 Open API 전환과 함께 이미 코드에서 통째로 삭제됐다 - 다나와는 스크래핑이라
// 데이터를 못 믿어 3개 모델의 교차검증이 필요했지만, 11번가는 공식 API라 데이터
// 자체가 이미 신뢰 가능해서 이제 필요한 건 "이게 정말 찾는 상품이 맞는지" 확인하는
// 관련성 검증(로컬 규칙 - 문자열·임베딩 유사도)뿐이다. 최종 판단도 여러 모델이
// 아니라 하나(Qwen)가 가격·리뷰·구매만족도를 함께 보고 내린다. 질의 정제 모델은
// Qwen에서 HCX로 교체 예정이라 문구를 미리 HCX로 반영해둔다(실제 코드 전환은 별도
// 작업).
const steps = [
  {
    icon: Sparkles,
    title: '질의 정제 & 가격 조건 분리',
    description: 'HCX가 대화체 문장에서 실제 찾는 상품명만 추려내고, "2만원대"처럼 가격 조건이 있으면 검색어와 분리해 따로 기억해둡니다.',
  },
  {
    icon: Search,
    title: '11번가 공식 데이터 조회',
    description: '11번가 오픈 API로 실시간 판매 정보를 직접 받아옵니다. 상품명, 가격, 리뷰가 처음부터 신뢰할 수 있는 데이터입니다.',
  },
  {
    icon: ShieldCheck,
    title: '관련성 검증',
    description: '검색 결과 중 실제로 찾는 상품이 맞는지 상품명 유사도와 의미 유사도로 확인해, 이름만 비슷한 무관한 상품을 걸러냅니다.',
  },
  {
    icon: SlidersHorizontal,
    title: '가격 조건 필터 & 정렬',
    description: '앞서 분리해둔 가격 조건에 맞는 후보만 남기고, 검색어와 얼마나 관련 있는 상품인지 순서대로 정렬합니다.',
  },
  {
    icon: Scale,
    title: '추천 Agent가 최종 판단',
    description: '검증된 후보 중 가격뿐 아니라 리뷰 수와 구매만족도까지 함께 보고, Qwen이 가장 추천할 만한 상품을 고릅니다.',
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
              추측하지 않습니다. 11번가 공식 데이터로 실제로 판매 중인 상품인지부터 확인하고, 그중 검증된
              후보만 <span className="font-medium" style={{ color: '#4ADE80' }}>Qwen</span>이 가격과 리뷰,
              구매만족도까지 함께 보고 답합니다.
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
            aria-label="검색어가 들어오면 HCX가 질의를 정제하면서 가격 조건을 따로 떼어낸다. 정제된 검색어로 11번가 공식 오픈 API를 직접 조회해 실제 판매 데이터를 받는다. 그 결과를 상품명 유사도와 의미 유사도로 관련성 검증한 뒤, 앞서 떼어둔 가격 조건으로 필터링하고 관련도순으로 정렬한다. 마지막으로 Qwen 하나가 가격, 리뷰, 구매만족도를 함께 보고 최종 추천 하나와 다른 후보들의 이유를 만든다."
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
            <rect x="16" y="60" width="150" height="90" rx="12" fill="#ffffff" stroke="#e5e5e5" />
            <text x="91" y="98" textAnchor="middle" fontSize="13" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">검색어 입력</text>
            <text x="91" y="118" textAnchor="middle" fontSize="10" fill="#8a8a8a" fontFamily="-apple-system, sans-serif">"망고주스 2만원대"</text>

            <line x1="166" y1="105" x2="200" y2="105" stroke="#c7c7c7" strokeWidth="1.4" markerEnd="url(#hwc-arrow)" />

            {/* Refine + price split (HCX) */}
            <rect x="200" y="60" width="180" height="90" rx="12" fill="#ffffff" stroke="#e5e5e5" />
            <text x="290" y="94" textAnchor="middle" fontSize="12.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">질의 정제 ·</text>
            <text x="290" y="110" textAnchor="middle" fontSize="12.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">가격 조건 분리</text>
            <text x="290" y="128" textAnchor="middle" fontSize="10" fill="#8a8a8a" fontFamily="-apple-system, sans-serif">HCX</text>

            <line x1="380" y1="105" x2="414" y2="105" stroke="#c7c7c7" strokeWidth="1.4" markerEnd="url(#hwc-arrow)" />

            {/* 11st official API */}
            <rect x="414" y="60" width="170" height="90" rx="12" fill="#ffffff" stroke="#e5e5e5" />
            <text x="499" y="98" textAnchor="middle" fontSize="12.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">11번가 공식 API</text>
            <text x="499" y="118" textAnchor="middle" fontSize="10" fill="#8a8a8a" fontFamily="-apple-system, sans-serif">실시간 판매 데이터</text>

            <line x1="584" y1="105" x2="618" y2="105" stroke="#c7c7c7" strokeWidth="1.4" markerEnd="url(#hwc-arrow)" />

            {/* Relevance guard */}
            <rect x="618" y="60" width="170" height="90" rx="12" fill="#ffffff" stroke="#e5e5e5" />
            <text x="703" y="98" textAnchor="middle" fontSize="12.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">관련성 검증</text>
            <text x="703" y="118" textAnchor="middle" fontSize="10" fill="#8a8a8a" fontFamily="-apple-system, sans-serif">문자열 · 임베딩 유사도</text>

            <line x1="788" y1="105" x2="822" y2="105" stroke="#c7c7c7" strokeWidth="1.4" markerEnd="url(#hwc-arrow)" />

            {/* Price filter + sort */}
            <rect x="822" y="60" width="170" height="90" rx="12" fill="#ffffff" stroke="#e5e5e5" />
            <text x="907" y="94" textAnchor="middle" fontSize="12.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">가격 조건 필터</text>
            <text x="907" y="110" textAnchor="middle" fontSize="12.5" fontWeight="600" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">· 관련도 정렬</text>
            <text x="907" y="128" textAnchor="middle" fontSize="10" fill="#8a8a8a" fontFamily="-apple-system, sans-serif">규칙 기반</text>

            <line x1="992" y1="105" x2="1026" y2="105" stroke="#c7c7c7" strokeWidth="1.4" markerEnd="url(#hwc-arrow)" />

            {/* Recommend agent (Qwen) */}
            <rect x="1026" y="50" width="170" height="110" rx="14" fill="rgba(74,222,128,0.08)" stroke="#4ADE80" strokeWidth="1.6" />
            <text x="1111" y="94" textAnchor="middle" fontSize="14" fontWeight="700" fill="#0a0a0a" fontFamily="-apple-system, sans-serif">추천 Agent</text>
            <text x="1111" y="112" textAnchor="middle" fontSize="10.5" fill="#166534" fontFamily="-apple-system, sans-serif">Qwen</text>
            <text x="1111" y="128" textAnchor="middle" fontSize="9.5" fill="#166534" fontFamily="-apple-system, sans-serif">가격 · 리뷰 · 만족도</text>

            <line x1="1196" y1="105" x2="1230" y2="105" stroke="#4ADE80" strokeWidth="1.6" markerEnd="url(#hwc-arrow-accent)" />

            {/* Final */}
            <rect x="1230" y="50" width="120" height="110" rx="14" fill="#0a0a0a" />
            <text x="1290" y="90" textAnchor="middle" fontSize="13" fontWeight="600" fill="#ffffff" fontFamily="-apple-system, sans-serif">최종 추천</text>
            <text x="1290" y="108" textAnchor="middle" fontSize="9.5" fill="#c7c7c7" fontFamily="-apple-system, sans-serif">상품 · 가격</text>
            <text x="1290" y="122" textAnchor="middle" fontSize="9.5" fill="#c7c7c7" fontFamily="-apple-system, sans-serif">판매처 · 근거</text>

            <text x="16" y="196" fontSize="10.5" fill="#a3a3a3" fontFamily="ui-monospace, monospace" letterSpacing="0.02em">
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
