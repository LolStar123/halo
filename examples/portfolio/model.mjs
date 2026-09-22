const stop=new Set('a an the and or but to of in on at for is are was were be been this that it its with what how why when who should would could must will we our you your do does did can have has from as by'.split(' '));
export const tokens=text=>(String(text).toLowerCase().match(/[a-z0-9]+/g)||[]).filter(w=>!stop.has(w));
export function chunks(docs){
    const result=[];for(const [d,doc] of docs.entries()){
        if(typeof doc.title!=='string'||typeof doc.text!=='string')throw Error('Notes need a title and text.');
        let heading=doc.title;for(const block of doc.text.split(/\n\s*\n/)){
            if(/^#{1,6}\s/.test(block)){const lines=block.split('\n');heading=lines.shift().replace(/^#+\s*/,'');const text=lines.join('\n').trim();if(text)result.push({id:`${d}:${result.length}`,source:doc.title,heading,text});}
            else if(block.trim())result.push({id:`${d}:${result.length}`,source:doc.title,heading,text:block.trim()});
        }
    }return result;
}
export function retrieve(docs,query,limit=5){
    const terms=[...new Set(tokens(query))];if(!terms.length)return [];
    const rows=chunks(docs).map(c=>({...c,words:tokens(c.heading+' '+c.text)})),average=rows.reduce((n,c)=>n+c.words.length,0)/Math.max(1,rows.length);
    return rows.map(c=>{let score=0,matched=[];for(const term of terms){const freq=c.words.filter(w=>w===term).length;if(!freq)continue;const count=rows.filter(r=>r.words.includes(term)).length,idf=Math.log(1+(rows.length-count+.5)/(count+.5));score+=idf*freq*2.2/(freq+1.2*(.25+.75*c.words.length/average));matched.push(term);}return {...c,score,matched}}).filter(c=>c.score>0).sort((a,b)=>b.score-a.score).slice(0,limit);
}
export function sentences(text) {
    const parts = typeof Intl.Segmenter === 'function'
        ? [...new Intl.Segmenter('en', {granularity:'sentence'}).segment(text)].map(s=>s.segment.trim()).filter(Boolean)
        : String(text).split(/(?<=[.!?])\s+(?=[A-Z])/).map(s=>s.trim()).filter(Boolean);
    const result=[];
    for (const part of parts) {
        if(result.length && /\b(?:Mr|Mrs|Ms|Dr|Prof|e\.g|i\.e|[A-Z])\.$/.test(result.at(-1))) result[result.length-1]+=' '+part;
        else result.push(part);
    }
    return result;
}
export function extractCue(results){return results.slice(0,3).map(r=>r.text).join('\n\n');}
