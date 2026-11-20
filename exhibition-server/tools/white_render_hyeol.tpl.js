(function(){
const V = __VIEW__;
const c=document.getElementById('gl'); c.style.cssText='position:fixed;left:0;top:0;width:__W__px;height:__H__px;z-index:999';
c.width=__W__; c.height=__H__;
WOOD[0]=0.25; WOOD[1]=0.25; WOOD[2]=0.26; delete models.hyeol; const md = modelOf('hyeol');
const d=devPose('hyeol'), hd=d.heading*Math.PI/180;
eye.on=false; cam.target=W(d.x, d.y, V.tz); cam.dist=V.dist; cam.pitch=V.pitch; cam.yaw=Math.PI-hd+V.dyaw;
gl.viewport(0,0,c.width,c.height); gl.clearColor(1,1,1,1); gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
gl.useProgram(prog); gl.enable(gl.DEPTH_TEST); gl.disable(gl.BLEND); gl.depthMask(true);
const m=mat(); gl.uniform3f(loc.ca, ...m.e); gl.uniform3f(loc.cb, ...cam.target); gl.uniform1f(loc.amb, 1);
gl.uniform1f(loc.lobby, 1); gl.uniform1f(loc.outdoor, 0);
{ const L = lampWorld(); gl.uniform3fv(loc.lamps, L.pos); gl.uniform1f(loc.nHall, L.nHall); gl.uniform1f(loc.nLamps, L.n); }
gl.uniform2f(loc.cut, 0, 1);
const h=hd, co=Math.cos(h), sn=Math.sin(h);
const Mo=[co,0,sn,0, 0,1,0,0, -sn,0,co,0, d.x/1000, 0, -d.y/1000, 1];
gl.uniformMatrix4fv(loc.mvp,false,new Float32Array(mul4(m.M,Mo))); gl.uniformMatrix4fv(loc.mdl,false,new Float32Array(Mo));
gl.uniform3f(loc.ibk,0,0,0.8); gl.uniform3f(loc.light, 0.35*co+0.5*sn, 0.8, -0.35*sn+0.5*co);
drawBatch(md.tri, gl.TRIANGLES, true); drawBatch(md.lines, gl.LINES, false);
gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); gl.depthMask(false);
if (md.shadow) drawBatch(md.shadow, gl.TRIANGLES, false);
gl.depthMask(true);
const u=c.toDataURL('image/png'); window.__stop=true; document.body.innerHTML='<img src="'+u+'" style="position:fixed;left:0;top:0;width:__W__px;height:__H__px">';
})();
