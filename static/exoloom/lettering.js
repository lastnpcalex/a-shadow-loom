/* Original portal metallic lettering algorithm, extracted without repainting it. */
(function () {
    'use strict';
    let viewer, posterFontReady = false;
    function loadPosterFont() {
        if (posterFontReady) return;
        posterFontReady = true;
        const font = new FontFace('Neon Teko', 'url("/static/exoloom/Teko.ttf")', {weight:'300 700'});
        document.fonts.add(font); font.load().catch(() => {});
    }
  const artifactGlyphs={
    B:[67,'M0 0H35Q65 0 65 25Q65 43 51 49Q67 56 67 76Q67 100 35 100H0Z M14 14V42H31Q49 42 49 27Q49 14 31 14Z M14 56V86H32Q51 86 51 72Q51 56 32 56Z'],
    E:[55,'M0 0H55V14H14V41H47V55H14V86H55V100H0Z'],
    Y:[65,'M0 0H16L32 39L49 0H65L40 58V100H25V58Z'],
    O:[68,'M34 0Q68 0 68 28V72Q68 100 34 100Q0 100 0 72V28Q0 0 34 0Z M34 14Q15 14 15 31V69Q15 86 34 86Q53 86 53 69V31Q53 14 34 14Z'],
    N:[67,'M0 0H16L52 68V0H67V100H51L15 33V100H0Z'],
    D:[67,'M0 0H31Q67 0 67 30V70Q67 100 31 100H0Z M15 14V86H30Q52 86 52 66V34Q52 14 30 14Z'],
    H:[64,'M0 0H14V42H50V0H64V100H50V56H14V100H0Z'],
    U:[66,'M0 0H15V69Q15 86 33 86Q51 86 51 69V0H66V72Q66 100 33 100Q0 100 0 72Z'],
    M:[80,'M0 0H17L40 63L63 0H80V100H65V34L40 95L15 34V100H0Z'],
    A:[72,'M27 0H45L72 100H56L49 76H23L16 100H0Z M36 23L27 62H45Z'],
    F:[55,'M0 0H55V14H14V41H47V55H14V100H0Z'],
    I:[22,'M4 0H18V100H4Z'],L:[55,'M0 0H15V86H55V100H0Z'],
    T:[64,'M0 0H64V14H40V100H24V14H0Z'],
    V:[68,'M0 0H16L34 74L52 0H68L42 100H26Z'],
    W:[94,'M0 0H15L27 74L40 0H54L67 74L79 0H94L76 100H59L47 34L35 100H18Z'],
    X:[66,'M0 0H17L33 34L49 0H66L42 49L66 100H49L33 65L17 100H0L24 49Z'],
    Z:[60,'M0 0H60V14L18 86H60V100H0V86L42 14H0Z'],
    P:[64,'M0 0H32Q64 0 64 28Q64 57 32 57H15V100H0Z M15 14V43H31Q49 43 49 28Q49 14 31 14Z'],
    R:[67,'M0 0H32Q64 0 64 28Q64 48 47 55L67 100H50L32 58H15V100H0Z M15 14V43H31Q49 43 49 28Q49 14 31 14Z'],
    C:[65,'M65 22L51 27Q49 14 33 14Q15 14 15 31V69Q15 86 33 86Q49 86 51 73L65 78Q61 100 33 100Q0 100 0 72V28Q0 0 33 0Q61 0 65 22Z'],
    G:[67,'M65 22L51 27Q49 14 33 14Q15 14 15 31V69Q15 86 33 86Q52 86 52 70V61H35V47H67V73Q67 100 33 100Q0 100 0 72V28Q0 0 33 0Q61 0 65 22Z'],
    J:[55,'M40 0H55V73Q55 100 27 100Q0 100 0 76V67H15V73Q15 86 27 86Q40 86 40 71Z'],
    K:[67,'M0 0H15V43L48 0H66L29 48L67 100H49L15 55V100H0Z'],
    Q:[70,'M34 0Q68 0 68 28V72Q68 88 57 94L70 106H51L43 99Q39 100 34 100Q0 100 0 72V28Q0 0 34 0Z M34 14Q15 14 15 31V69Q15 86 34 86Q53 86 53 69V31Q53 14 34 14Z'],
    S:[64,'M63 24L48 27Q47 14 32 14Q15 14 15 26Q15 36 35 43Q64 53 64 75Q64 100 32 100Q1 100 0 76L15 73Q16 86 32 86Q49 86 49 74Q49 63 29 56Q0 46 0 26Q0 0 32 0Q61 0 63 24Z']
  };
  function artifactLine(line,size,target,cx,cy,angular){
    const chars=[...line],glyphs=chars.map(ch=>artifactGlyphs[ch]);
    const widths=chars.map((ch,i)=>glyphs[i]?glyphs[i][0]:ch===' '?30:60),units=widths.reduce((a,b)=>a+b,0)+Math.max(0,chars.length-1)*5;
    if(target){let x=cx-units*size/200;chars.forEach((ch,i)=>{target.save();target.translate(x,cy-size/2);target.scale(size/100,size/100);
      if(glyphs[i]){let path=glyphs[i][1];
        if(angular&&i===0&&ch==='H')path='M0 0H14V42H50V0H64V100H50V56H14V100L0 125Z';
        if(angular&&i===chars.length-1&&ch==='N')path='M0 0H16L52 68V0H67V125L51 100L15 33V100H0Z';
        target.fill(new Path2D(path),'evenodd');
      }else if(ch!==' '){target.textAlign='left';target.textBaseline='alphabetic';const m=target.measureText(ch),height=m.actualBoundingBoxAscent+m.actualBoundingBoxDescent;target.translate(4,0);target.scale(52/Math.max(m.width,1),100/Math.max(height,1));target.fillText(ch,0,m.actualBoundingBoxAscent);}
      target.restore();x+=(widths[i]+5)*size/100;
    });}
    return units*size/100;
  }
  function drawLettering(targetCanvas, targetW, targetH){
    if(!viewer)return;
    const isCustom = targetCanvas && typeof targetCanvas.getContext === 'function';
    const canvas = isCustom ? targetCanvas : lettering;
    const c=viewer.params.caption;
    const w=(isCustom && targetW) ? targetW : Math.max(1,scene.clientWidth);
    const h=(isCustom && targetH) ? targetH : Math.max(1,scene.clientHeight);
    const dpr=isCustom?1:Math.min(devicePixelRatio,2);
    if(!isCustom){
      const key=JSON.stringify([c,w,h,dpr,posterFontReady,document.fonts.status,viewer.params.look.floor_palette,viewer.params.look.floor_color]);
      if(key===letteringKey)return;letteringKey=key;
      lettering.width=Math.round(w*dpr);lettering.height=Math.round(h*dpr);
      lettering.hidden=!c.enabled||!c.text;
      if(lettering.hidden)return;
    }
    const ctx=canvas.getContext('2d');
    ctx.save();
    if(!isCustom)ctx.scale(dpr,dpr);
    if((c.font==='Teko'||c.font==='Sculpted')&&!posterFontReady)loadPosterFont();
    const lines=(c.uppercase?c.text.toUpperCase():c.text).split(/[|\n]/).slice(0,6),size=h*c.size/100,lineHeight=size*c.line_gap;
    const sculpted=c.font==='Sculpted';
    ctx.font=`${c.font==='Teko'||sculpted?700:900} ${size}px ${c.font==='Teko'||sculpted?'"Neon Teko"':c.font==='Orbitron'?'Orbitron':'Impact, "Arial Black"'}, sans-serif`;
    const measured=Math.max(...lines.map(line=>sculpted?artifactLine(line,size):ctx.measureText(line).width),1);
    const maxW=w*c.width/100;
    const fit=Math.min(1.0,maxW/Math.max(measured*c.stretch,1));
    const scaleX=fit*c.stretch;
    const scaleY=fit;
    ctx.translate(w*c.x/100,h*c.y/100);ctx.scale(scaleX,scaleY);ctx.textAlign='center';ctx.textBaseline='middle';ctx.lineJoin='miter';ctx.miterLimit=2;
    lines.forEach((line,index)=>{
      const y=(index-(lines.length-1)/2)*lineHeight;
      const lineWidth=sculpted?artifactLine(line,size):ctx.measureText(line).width;
      const metrics=sculpted?{actualBoundingBoxAscent:size/2,actualBoundingBoxDescent:size/2,actualBoundingBoxLeft:lineWidth/2,actualBoundingBoxRight:lineWidth/2}:ctx.measureText(line),base=metrics.actualBoundingBoxDescent;
      function terminals(target,cx,cy){
        if(sculpted||!c.angular||index===0||!line.trim())return;
        const left=cx-metrics.actualBoundingBoxLeft,right=cx+metrics.actualBoundingBoxRight,bottom=cy+base;
        target.moveTo(left,bottom-size*.035);target.lineTo(left,bottom+size*.2);target.lineTo(left+size*.09,bottom-size*.035);target.closePath();
        target.moveTo(right,bottom-size*.035);target.lineTo(right,bottom+size*.2);target.lineTo(right-size*.09,bottom-size*.035);target.closePath();
      }
      const face=document.createElement('canvas'),fw=Math.ceil(lineWidth+size*.16),fh=Math.ceil(size*1.8);
      face.width=Math.ceil(fw*dpr);face.height=Math.ceil(fh*dpr);
      const fc=face.getContext('2d');fc.scale(dpr,dpr);fc.font=ctx.font;fc.textAlign='center';fc.textBaseline='middle';
      // One alpha silhouette joins the pointed feet to the glyphs before
      // beveling. Separate strokes left visible seams across their joins.
      fc.fillStyle='#fff';if(sculpted)artifactLine(line,size,fc,fw/2,fh/2,c.angular&&index>0);else fc.fillText(line,fw/2,fh/2);fc.beginPath();terminals(fc,fw/2,fh/2);fc.fill();
      const mask=document.createElement('canvas');mask.width=face.width;mask.height=face.height;mask.getContext('2d').drawImage(face,0,0);
      const top=fh/2-metrics.actualBoundingBoxAscent,bottom=fh/2+base,inkHeight=bottom-top;
      const bevel=size*.028*c.bevel,neon=viewer.params.look.floor_palette?viewer.params.look.floor_color:'#e879ef';
      function metalGradient(stops){const g=fc.createLinearGradient(0,top,0,bottom);stops.forEach(([at,color])=>g.addColorStop(at,color));return g;}
      function silhouette(radius,fill,dx=0,dy=0,blur=0){
        const layer=document.createElement('canvas');layer.width=face.width;layer.height=face.height;const lc=layer.getContext('2d');
        lc.drawImage(mask,0,0);
        if(radius)for(let i=0;i<24;i++){const a=i*Math.PI/12;lc.drawImage(mask,Math.cos(a)*radius*dpr,Math.sin(a)*radius*dpr);}
        lc.scale(dpr,dpr);lc.globalCompositeOperation='source-in';lc.fillStyle=fill;lc.fillRect(0,0,fw,fh);
        ctx.shadowColor=neon;ctx.shadowBlur=blur;ctx.drawImage(layer,-fw/2+dx,y-fh/2+dy,fw,fh);ctx.shadowBlur=0;
      }
      const style=c.style==='mixed'?(index===0?'chrome':'iridescent'):c.style;
      let sideColors,bevel1,bevel2,stops;
      if(style==='ice'){
        sideColors=[[0,'#0d1527'],[.45,'#182844'],[.65,'#0a101e'],[1,'#2c4568']];
        bevel1=[[0,'#f2f8ff'],[.25,'#5b7ea6'],[.5,'#d9ebfc'],[.7,'#334c69'],[1,'#b5d5f2']];
        bevel2=[[0,'#e6f2ff'],[.48,'#a6cbed'],[.55,'#41638a'],[1,'#e0f0ff']];
        stops=[[0,'#151d38'],[.24,'#354c73'],[.44,'#9ec8ed'],[.51,'#ffffff'],[.55,'#f0f7ff'],[.59,'#0f1c30'],[.69,'#16233d'],[.84,'#384870'],[1,'#8da3cf']];
      }else if(style==='ruby'){
        sideColors=[[0,'#24060d'],[.45,'#420d18'],[.65,'#180308'],[1,'#5c1422']];
        bevel1=[[0,'#ffedf0'],[.25,'#99243d'],[.5,'#ffb8c6'],[.7,'#591322'],[1,'#e8728a']];
        bevel2=[[0,'#ffe6eb'],[.48,'#f0899d'],[.55,'#7d1a2e'],[1,'#ffccd6']];
        stops=[[0,'#380a14'],[.24,'#8c162f'],[.44,'#f2687f'],[.51,'#fff0f3'],[.55,'#ffe0e6'],[.59,'#24060d'],[.69,'#3b0a16'],[.84,'#521436'],[1,'#c93c68']];
      }else if(style==='iridescent'){
        sideColors=[[0,'#221910'],[.45,'#31483b'],[.65,'#121915'],[1,'#7b4931']];
        bevel1=[[0,'#fff0cf'],[.25,'#b87351'],[.5,'#ffdeb5'],[.7,'#79402d'],[1,'#efbf8c']];
        bevel2=[[0,'#fff0d0'],[.48,'#f4cba1'],[.55,'#a15e43'],[1,'#ffe3bb']];
        stops=[[0,'#0e3026'],[.25,'#224a35'],[.44,'#87a568'],[.53,'#eef0ba'],[.58,'#f7f8d8'],[.62,'#182b24'],[.77,'#252535'],[.92,'#aa759d'],[1,'#e2a87b']];
      }else{
        sideColors=[[0,'#221910'],[.45,'#31483b'],[.65,'#121915'],[1,'#7b4931']];
        bevel1=[[0,'#fff0cf'],[.25,'#b87351'],[.5,'#ffdeb5'],[.7,'#79402d'],[1,'#efbf8c']];
        bevel2=[[0,'#fff0d0'],[.48,'#f4cba1'],[.55,'#a15e43'],[1,'#ffe3bb']];
        stops=[[0,'#5d3022'],[.24,'#a86242'],[.44,'#e7ad78'],[.51,'#fff4d2'],[.55,'#fff7da'],[.59,'#1a3427'],[.69,'#17291f'],[.84,'#46344c'],[1,'#c38b92']];
      }
      const side=metalGradient(sideColors);
      for(let i=5;i>0;i--)silhouette(bevel,side,size*.005*i,size*.007*i);
      if(c.glow){silhouette(bevel*1.1,neon,0,0,size*.045*c.glow);ctx.globalAlpha=.18;silhouette(bevel*1.1,neon,0,0,size*.12*c.glow);ctx.globalAlpha=1;}
      if(bevel){
        silhouette(bevel,metalGradient(bevel1));
        silhouette(bevel*.74,style==='ice'?'#1b283d':style==='ruby'?'#3d0c15':'#48291f');
        silhouette(bevel*.44,metalGradient(bevel2));
        silhouette(bevel*.15,style==='ice'?'#182436':style==='ruby'?'#330810':'#44291e');
      }
      fc.clearRect(0,0,fw,fh);fc.globalCompositeOperation='source-over';
      // The reflected skyline undulates gently across the metal face; its
      // shading is anchored to actual ink bounds, not the font's em box.
      for(let x=0;x<fw;x+=2){const wave=inkHeight*.022*(Math.sin(x/fw*32)+.45*Math.sin(x/fw*61));const g=fc.createLinearGradient(0,top+wave,0,bottom+wave);stops.forEach(([at,color])=>g.addColorStop(at,color));fc.fillStyle=g;fc.fillRect(x,0,2,fh);}
      fc.globalCompositeOperation='destination-in';fc.drawImage(mask,0,0,fw,fh);
      if(c.tint_color&&c.tint_color.toLowerCase()!=='#ffffff'){
        fc.save();fc.globalCompositeOperation='color';fc.fillStyle=c.tint_color;fc.globalAlpha=.5;fc.fillRect(0,0,fw,fh);fc.restore();
      }
      fc.globalCompositeOperation='source-atop';
      if(c.metal){
        const shine=fc.createLinearGradient(0,0,fw,fh*.6);[[0,'rgba(255,250,223,0)'],[.38,`rgba(255,250,223,${c.metal*.03})`],[.47,`rgba(255,250,223,${c.metal*.23})`],[.52,'rgba(255,250,223,0)'],[1,'rgba(255,250,223,0)']].forEach(([at,color])=>shine.addColorStop(at,color));fc.fillStyle=shine;fc.fillRect(0,0,fw,fh);
      }
      if(c.grid&&style!=='iridescent'){const rgb=parseInt(neon.slice(1),16);fc.strokeStyle=`rgba(${rgb>>16&255},${rgb>>8&255},${rgb&255},${c.grid*.55})`;fc.lineWidth=Math.max(.4,size*.004);const horizon=top+inkHeight*.68;
        fc.beginPath();for(let i=1;i<=5;i++){const gy=horizon+(bottom-horizon)*Math.pow(i/5,1.7);fc.moveTo(0,gy);fc.lineTo(fw,gy);}
        for(let i=-9;i<=9;i++){fc.moveTo(fw*.5+i*size*.11,horizon);fc.lineTo(fw*.5+i*size*.48,bottom+size*.2);}fc.stroke();
      }
      if(c.fuzz){const grain=document.createElement('canvas');grain.width=grain.height=64;const gc=grain.getContext('2d'),pixels=gc.createImageData(64,64);let seed=9217;
        for(let i=0;i<pixels.data.length;i+=4){seed=(Math.imul(seed,1664525)+1013904223)|0;const value=seed>>>24;pixels.data[i]=pixels.data[i+1]=pixels.data[i+2]=value;pixels.data[i+3]=Math.round(c.fuzz*32);}
        gc.putImageData(pixels,0,0);fc.fillStyle=fc.createPattern(grain,'repeat');fc.fillRect(0,0,fw,fh);
      }
      ctx.drawImage(face,-fw/2,y-fh/2,fw,fh);
    });
    ctx.restore();
  }

    window.EXOLettering = {
        draw(canvas, text, width, height, options = {}) {
            viewer = {params:{caption:{enabled:true, text, style:'ice', font:'Orbitron',
                size:64, x:50, y:50, width:93, uppercase:false, bevel:1, grid:.85,
                stretch:1, line_gap:1.12, angular:false, metal:.65, glow:.85,
                fuzz:.22, tint_color:'#ffffff', ...options},
                look:{floor_palette:true, floor_color:'#00ffff'}}};
            canvas.width = width; canvas.height = height;
            drawLettering(canvas, width, height);
        }
    };
})();
