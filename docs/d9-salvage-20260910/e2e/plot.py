"""Standalone publication-style SVG from saved grouped E2E predictions."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

def main():
    rows = [json.loads(s) for s in (HERE/'predictions.jsonl').read_text().splitlines()]
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="960" height="640" viewBox="0 0 960 640">',
             '<rect width="960" height="640" fill="white"/>',
             '<g font-family="Arial, sans-serif" font-weight="bold" fill="#172234">',
             '<text x="100" y="42" font-size="25">Historical conditional E2E prediction</text>',
             '<text x="100" y="72" font-size="16">819 runs · 545 instance groups · out-of-fold development predictions</text>']
    x0,y0,w,h = 100,120,800,380
    for pct in range(0,101,20):
        y=y0+h-h*pct/100
        parts.extend([f'<path d="M{x0},{y} H{x0+w}" stroke="#d8dde5"/>',
                      f'<text x="85" y="{y+6}" text-anchor="end" font-size="16">{pct}</text>'])
    for pct in range(0,201,25):
        x=x0+w*pct/200
        parts.append(f'<text x="{x}" y="526" text-anchor="middle" font-size="16">{pct}</text>')
    x=x0+w*25/200
    parts.extend([f'<path d="M{x},{y0} V{y0+h}" stroke="#a73532" stroke-width="2" stroke-dasharray="7,5"/>',
                  f'<text x="{x+8}" y="140" font-size="14" fill="#a73532">25% threshold</text>'])
    for key,color,label,offset in [('global_median','#747b86','Median baseline',0),
                                    ('conditional_relative_nnls','#126e82','Relative-error NNLS',1)]:
        errors=sorted(abs(r[key]-r['observed_ms'])/r['observed_ms']*100 for r in rows)
        points=[f'{x0},{y0+h}']
        points.extend(f'{x0+w*e/200:.2f},{y0+h-h*(i+1)/len(errors):.2f}'
                      for i,e in enumerate(errors) if e<=200)
        points.append(f'{x0+w},{y0+h-h*sum(e<=200 for e in errors)/len(errors):.2f}')
        parts.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="{color}" stroke-width="3"/>')
        parts.extend([f'<path d="M560,{165+offset*28} h32" stroke="{color}" stroke-width="4"/>',
                      f'<text x="603" y="{171+offset*28}" font-size="17">{label}</text>'])
    parts.extend([f'<rect x="{x0}" y="{y0}" width="{w}" height="{h}" fill="none" stroke="#172234" stroke-width="2"/>',
                  '<text x="500" y="565" text-anchor="middle" font-size="19">Absolute relative E2E error (%)</text>',
                  '<text transform="translate(30,310) rotate(-90)" text-anchor="middle" font-size="19">Runs at or below error (%)</text>',
                  '<text x="100" y="603" font-size="14">Declared action/token counts; not a prospective forecast or an individual-event D9 pass.</text>',
                  '</g></svg>'])
    (HERE/'e2e_error_cdf.svg').write_text('\n'.join(parts)+'\n')

if __name__ == '__main__': main()
