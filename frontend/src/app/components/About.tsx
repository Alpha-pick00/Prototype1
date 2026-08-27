import { useRef } from 'react';
import { motion, useScroll, useTransform } from 'motion/react';
import weCraftImage from '../../assets/about/we-craft.jpg';

import qwenLogo from '../../assets/about/logos/qwen.svg';
import deepseekLogo from '../../assets/about/logos/deepseek.svg';
import awsLogo from '../../assets/about/logos/aws.svg';
import elevenstLogo from '../../assets/about/logos/11st.png';

// "gpt" 에이전트 슬롯은 HCX(HyperCLOVA X)가 담당한다(backend/app/agents/gpt.py
// 참고 - 원래 Qwen이었다가 2026-08-25부터 한국어 이해도/효용성 때문에
// 의도적으로 HCX로 전환, 임시 조치 아님). qwen.svg는 lobehub/lobe-icons(MIT,
// AI 서비스 attribution 용도로 만든 오픈소스 아이콘셋)에서 가져온 것 - Qwen
// 공식 자산이 아니라 커뮤니티 아이콘이다. Qwen은 여전히 임베딩(관련도 랭킹)과
// judge(최종 추천 선택)를 담당해 실사용 비중이 커 계속 노출한다.
//
// 11st.png는 11번가 공식 브랜드 가이드(design.11stcorp.com/brand/logos,
// 2026-08-25 확인)에서 받은 "11STREET Identity" 스크린용 배포 자산 그대로다 -
// 다나와 시절부터 있던 미사용 11st.webp(출처 불명)는 안 쓴다. Black/White/
// 11Gradiant 세 버전 중 11Gradiant(가이드가 명시한 메인 컬러)를 쓴다 - 이
// 마퀴의 다른 로고들처럼 평소엔 그레이스케일로 죽어있다가 호버하면 원래
// 색이 살아나는데, Black을 쓰면 그레이스케일이든 아니든 그냥 검정이라 이
// 호버 효과가 다른 로고들과 다르게 안 먹혔다. 가이드의 금지규정(색상·형태·
// 비례 변형 금지, 약칭 "11ST"/"11st" 단독 사용 금지)에 따라 이 파일을
// 리컬러/리사이즈 없이 원본 그대로만 쓴다.
//
// HCX는 로고 없이 텍스트로만 노출한다(2026-08-27) - 네이버 공식 CLOVA Studio
// 브랜드 가이드(guide.ncloud-docs.com/docs/clovastudio-brand-guideline)가
// "HyperCLOVA X_Logotype.zip"/"Powered by HyperCLOVA X_Logotype.zip" 배포
// 자산을 명시하고 있지만, 이 저장소 환경에서 실제 다운로드 URL에 접근이
// 안 돼(리소스 없음 응답) 자산 파일을 확보하지 못했다. 정확도(실제로 4개
// LLM 중 하나로 쓰이는 사실)를 우선해 로고 없이 카드로 먼저 반영하고,
// 나중에 파일을 구하면 logo 필드만 채우면 된다 - logo가 없으면
// PoweredByCard가 자동으로 텍스트 전용 스타일로 렌더링한다.
const poweredByClients: { name: string; url: string; logo?: string }[] = [
  { name: '11번가', url: 'https://www.11st.co.kr', logo: elevenstLogo },
  { name: 'Qwen', url: 'https://qwenlm.ai', logo: qwenLogo },
  { name: 'DeepSeek', url: 'https://www.deepseek.com', logo: deepseekLogo },
  { name: 'HyperCLOVA X', url: 'https://clova.ai/clova-studio' },
  { name: 'Amazon AWS', url: 'https://aws.amazon.com', logo: awsLogo },
];

export const About = () => {
  const containerRef = useRef(null);

  const { scrollYProgress } = useScroll({
    target: containerRef,
    offset: ["start end", "end start"]
  });

  const opacity = useTransform(scrollYProgress, [0, 0.3], [0, 1]);

  return (
    <section ref={containerRef} id="about" className="py-32 relative bg-white overflow-hidden">
      {/* Background Grid - Technical Texture */}
      <div className="absolute inset-0 bg-[linear-gradient(to_right,#00000008_1px,transparent_1px),linear-gradient(to_bottom,#00000008_1px,transparent_1px)] bg-[size:4rem_4rem] [mask-image:radial-gradient(ellipse_60%_50%_at_50%_50%,#000_70%,transparent_100%)] pointer-events-none" />

      <div className="container mx-auto px-6">
        
        {/* Section Header - Consistent Style */}
        <div className="flex items-center gap-6 mb-24">
           <div className="flex items-baseline gap-3">
              <span className="font-serif italic text-lg text-neutral-950">01</span>
              <span className="text-xs font-mono uppercase tracking-[0.3em] text-neutral-600">About <span style={{ color: 'rgb(64,117,38)' }}>αlpha Pick</span></span>
           </div>
           <div className="h-px w-32 bg-gradient-to-r from-black/30 to-transparent" />
        </div>

        <div className="grid lg:grid-cols-[1.2fr_0.8fr] gap-20 items-start">

          {/* Text Content */}
          <div className="relative z-10 min-w-0">
            <motion.h2 
              initial={{ opacity: 0, y: 100 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.8, ease: [0.16, 1, 0.3, 1] }}
              className="text-5xl md:text-8xl font-medium tracking-tighter mb-12 leading-[0.9]"
            >
              We craft <br />
              <span className="italic font-serif" style={{ color: '#4ADE80' }}>smarter</span> choices.
            </motion.h2>

            <div className="grid sm:grid-cols-3 gap-10">
              {[
                {
                  label: '소음 대신 답',
                  text: '수십 개의 창, 수십 개의 가격. 우리는 그중 하나만 골라 보여드립니다.',
                },
                {
                  label: '근거 있는 추천',
                  text: '이유 없는 가격은 의미 없습니다. 모든 추천에는 근거가 함께합니다.',
                },
                {
                  label: '시간을 위한 서비스',
                  text: '돈만큼 시간도 소중하니까요. 확인은 저희가, 선택은 당신이 합니다.',
                },
              ].map((item, index) => (
                <motion.div
                  key={item.label}
                  initial={{ opacity: 0, y: 20 }}
                  whileInView={{ opacity: 1, y: 0 }}
                  viewport={{ once: true }}
                  transition={{ delay: 0.2 + index * 0.1, duration: 0.8 }}
                  className="space-y-3"
                >
                  <h4 className="text-xs font-mono uppercase tracking-widest" style={{ color: '#4ADE80' }}>
                    {item.label}
                  </h4>
                  <p className="text-xl font-light text-neutral-700 leading-snug">
                    {item.text}
                  </p>
                </motion.div>
              ))}
            </div>

            {/* Stats & Trust */}
            <div className="mt-16 pt-16 border-t border-black/5">
               <div className="grid grid-cols-3 gap-8 mb-16">
                 <div className="space-y-2 border-r border-black/5">
                   <h4 className="text-4xl font-light text-neutral-950">2026</h4>
                   <p className="text-xs uppercase tracking-widest text-neutral-500">Launching</p>
                 </div>
                 <div className="space-y-2 border-r border-black/5">
                   <h4 className="text-4xl font-light text-neutral-950">15<span className="text-neutral-400 text-lg">+</span></h4>
                   <p className="text-xs uppercase tracking-widest text-neutral-500">Platforms Compared</p>
                 </div>
                 <div className="space-y-2">
                   <h4 className="text-4xl font-light text-neutral-950">100K</h4>
                   <p className="text-xs uppercase tracking-widest text-neutral-500">Target Users by Year 3</p>
                 </div>
               </div>
            </div>
          </div>

          {/* Image Area */}
          <motion.div 
            style={{ opacity }}
            className="relative lg:mt-24"
          >
            <div className="relative z-10">
               <motion.div 
                 whileHover={{ scale: 0.98 }}
                 transition={{ duration: 0.5 }}
                 className="aspect-[4/5] overflow-hidden grayscale hover:grayscale-0 transition-all duration-700 ease-in-out bg-neutral-100"
               >
                 <img
                   src={weCraftImage}
                   alt="Workspace"
                   className="w-full h-full object-cover opacity-80"
                 />
                 <div className="absolute inset-0 bg-gradient-to-t from-black/50 to-transparent" />
               </motion.div>
               
               {/* Decorative Ring */}
               <div className="absolute -bottom-12 -left-12 w-48 h-48 border border-black/10 rounded-full flex items-center justify-center backdrop-blur-sm hidden md:flex" style={{ animation: 'spin 15s linear infinite' }}>
                 <style dangerouslySetInnerHTML={{__html: `
                   @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
                 `}} />
                 <svg className="w-full h-full p-2" viewBox="0 0 100 100">
                   <path id="circlePath" d="M 50, 50 m -37, 0 a 37,37 0 1,1 74,0 a 37,37 0 1,1 -74,0" fill="transparent" />
                   <text className="fill-neutral-500 text-[10px] uppercase tracking-widest font-mono">
                     <textPath href="#circlePath">
                       - Price Comparison • AI Curation • Smart Shopping
                     </textPath>
                   </text>
                 </svg>
               </div>
            </div>
          </motion.div>

        </div>

        {/* Powered By - AI 모델 + 인프라 로고를 한 줄 스캐닝 목록으로 보여준다 */}
        <div className="mt-20 pt-16 border-t border-black/5">
          <span className="text-base font-mono uppercase tracking-widest text-neutral-400 block mb-6">Powered by</span>
          <div className="relative w-full overflow-hidden [mask-image:linear-gradient(to_right,transparent,black_8%,black_92%,transparent)]">
            <div
              className="marquee-track flex w-max items-center gap-4 whitespace-nowrap"
              style={{ animation: 'marquee 40s linear infinite' }}
            >
              {[...poweredByClients, ...poweredByClients].map((client, i) => (
                <a
                  key={`${client.name}-${i}`}
                  href={client.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={client.name}
                  className="shrink-0 flex items-center gap-3 h-16 px-6 rounded-xl bg-neutral-100 grayscale opacity-70 hover:grayscale-0 hover:opacity-100 transition-all cursor-pointer"
                >
                  {client.logo ? (
                    <img src={client.logo} alt="" className="h-6 md:h-7 w-auto max-w-[32px] object-contain shrink-0" />
                  ) : (
                    <span className="h-6 md:h-7 w-8 shrink-0 flex items-center justify-center text-[10px] font-mono font-semibold tracking-wider text-neutral-500 border border-neutral-300 rounded">
                      AI
                    </span>
                  )}
                  <span className="text-sm font-light tracking-wide text-neutral-700 whitespace-nowrap">
                    {client.name}
                  </span>
                </a>
              ))}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
};
