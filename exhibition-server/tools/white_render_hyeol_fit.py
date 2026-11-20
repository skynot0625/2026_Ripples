import sys,glob,numpy as np
from PIL import Image
D,O,pre=sys.argv[1:4]
old=sorted(glob.glob('outputs/sim-renders-20261113/v2/02_흰배경/혈_*.png'))
names=['혈_A_눈높이정면','혈_B_눈높이측면','혈_C_높은사선']
def bbox(im,thr=110):
    a=np.asarray(im.convert('RGB')).min(axis=2)<thr; ys,xs=np.where(a); return xs.min(),xs.max(),ys.min(),ys.max()
for i,n in enumerate(names):
    ref=Image.open(old[i]); W,H=ref.size; bx=bbox(ref)
    im=Image.open(f'{D}/w/{pre}{i}.png').convert('RGB'); a=np.asarray(im).astype(float)
    r,g,b=a[...,0],a[...,1],a[...,2]; w=np.clip((r-b-10)/35,0,1)*(r>g)*(g>b); L=0.299*r+0.587*g+0.114*b; d=L*0.3+6
    for k,c in enumerate((r,g,b)): a[...,k]=c*(1-w)+d*w
    im=Image.fromarray(a.clip(0,255).astype('uint8')); m=bbox(im)
    s=(m[3]-m[2])/(bx[3]-bx[2]) if i<2 else (((m[3]-m[2])/(bx[3]-bx[2]))*((m[1]-m[0])/(bx[1]-bx[0])))**0.5
    cx_ref=(bx[0]+bx[1])/2; cy_ref=(bx[2]+bx[3])/2; cx=(m[0]+m[1])/2; cy=(m[2]+m[3])/2
    x0=cx-cx_ref*s; y0=cy-cy_ref*s
    bg=Image.new('RGB',(im.width+4000,im.height+4000),'white'); bg.paste(im,(2000,2000))
    out=bg.crop((int(x0)+2000,int(y0)+2000,int(x0+W*s)+2000,int(y0+H*s)+2000)).resize((W*2,H*2),Image.LANCZOS)
    out.save(f'{O}/{n}_상판검정_U컷.png')
