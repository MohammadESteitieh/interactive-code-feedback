const data=JSON.parse(document.getElementById('review-data').textContent);
const comments=new Map();
let active=0;
let hoverTimer=null;
let hoverRequest=0;
const files=document.getElementById('files');
const code=document.getElementById('code');
const count=document.getElementById('count');
const hover=document.getElementById('hover');
const hscroll=document.getElementById('hscroll');
const hscrollContent=document.getElementById('hscroll-content');
const fontValue=document.getElementById('font-value');
let codeFontSize=14;
try { codeFontSize=Number(localStorage.getItem('code-review-font-size'))||14; } catch(_error) {}

document.getElementById('scope').textContent=data.scope+' · '+data.root;
document.getElementById('capabilities').textContent='Syntax: '+data.syntax_engine+' · Hover: '+(data.hover_enabled?'local LSP':'off')+' · Notebooks: rendered';

function updateCount() {
  const n=[...comments.values()].filter(x=>x.trim()).length;
  count.textContent=n+(n===1?' comment':' comments');
}

function updateHorizontalScroll() {
  hscrollContent.style.width=Math.max(code.scrollWidth,code.clientWidth)+'px';
  hscroll.scrollLeft=code.scrollLeft;
}

function setCodeFontSize(value) {
  codeFontSize=Math.max(10,Math.min(24,value));
  code.style.setProperty('--code-size',codeFontSize+'px');
  code.style.setProperty('--code-line',Math.max(24,codeFontSize+16)+'px');
  fontValue.textContent=codeFontSize+'px';
  try { localStorage.setItem('code-review-font-size',String(codeFontSize)); } catch(_error) {}
  requestAnimationFrame(updateHorizontalScroll);
}

function renderFiles() {
  files.replaceChildren();
  data.files.forEach((file,index)=>{
    const button=document.createElement('button');
    button.className='file'+(index===active?' active':'');
    button.textContent=file.path;
    button.title=file.path;
    button.onclick=()=>{ active=index; renderFiles(); renderCode(); };
    files.appendChild(button);
  });
}

function editorFor(line,onClose) {
  const wrap=document.createElement('div');
  wrap.className='editor';
  const head=document.createElement('div');
  head.className='editor-head';
  const label=document.createElement('span');
  label.textContent='Line comment';
  const close=document.createElement('button');
  close.className='editor-close';
  close.type='button';
  close.textContent='Close';
  close.title='Close comment box (comment is kept)';
  close.onclick=onClose;
  head.append(label,close);
  const textarea=document.createElement('textarea');
  textarea.placeholder='Comment on this line';
  textarea.value=comments.get(line.id)||'';
  textarea.oninput=()=>{ comments.set(line.id,textarea.value); updateCount(); };
  textarea.onkeydown=event=>{
    if(event.key==='Escape') { event.preventDefault(); onClose(); }
  };
  wrap.append(head,textarea);
  return wrap;
}

function renderSource(source,line) {
  if(!line.tokens?.length) {
    source.textContent=line.text;
    return;
  }
  line.tokens.forEach(token=>{
    if(!token.text) return;
    if(!token.class && !token.hoverable) {
      source.appendChild(document.createTextNode(token.text));
      return;
    }
    const span=document.createElement('span');
    span.textContent=token.text;
    if(token.class) span.className='tok tok-'+token.class;
    if(token.hoverable) span.dataset.column=String(token.column);
    source.appendChild(span);
  });
}

function appendReviewLine(container,file,line) {
  const row=document.createElement('div');
  row.className='row '+line.kind;
  row.dataset.file=file.path;
  if(line.new_line!=null) row.dataset.newLine=String(line.new_line);

  const number=document.createElement('span');
  number.className='num';
  number.textContent=line.cell_line??line.new_line??line.old_line??'';
  if(line.cell_index!=null) number.title='Cell '+(line.cell_index+1)+', line '+line.cell_line;
  else number.title=line.new_line!=null?'New line '+line.new_line:(line.old_line!=null?'Old line '+line.old_line:'');

  const toggle=document.createElement('button');
  toggle.className='comment-toggle'+(comments.get(line.id)?.trim()?' has':'');
  toggle.textContent='✎';
  toggle.title='Add or edit a comment on this line';
  toggle.setAttribute('aria-label','Add or edit a comment on this line');

  const source=document.createElement('pre');
  source.className='source';
  renderSource(source,line);
  row.append(number,toggle,source);

  let open=false;
  let editor=null;
  const flip=()=>{
    open=!open;
    if(open) {
      editor=editorFor(line,flip);
      row.after(editor);
      editor.querySelector('textarea').focus();
    } else if(editor) {
      editor.remove();
      editor=null;
    }
  };
  toggle.onclick=flip;
  source.onclick=flip;
  container.appendChild(row);
  if(comments.get(line.id)?.trim()) {
    open=true;
    editor=editorFor(line,flip);
    container.appendChild(editor);
  }
}

function renderNotebookOutput(output) {
  const wrap=document.createElement('div');
  wrap.className='cell-output '+output.kind;
  const label=document.createElement('div');
  label.className='output-label';
  label.textContent=output.label||'output';
  wrap.appendChild(label);
  if(output.kind==='image') {
    const image=document.createElement('img');
    image.src='data:'+output.mime+';base64,'+output.data;
    image.alt='Notebook output';
    wrap.appendChild(image);
  } else if(output.kind==='html'||output.kind==='markdown') {
    const rich=document.createElement('div');
    rich.className='rich-output';
    rich.innerHTML=output.html||'';
    wrap.appendChild(rich);
  } else {
    const text=document.createElement('pre');
    text.textContent=output.text||'';
    wrap.appendChild(text);
  }
  return wrap;
}

function renderNotebook(file) {
  const notebook=document.createElement('div');
  notebook.className='notebook';
  const banner=document.createElement('div');
  banner.className='notebook-banner';
  banner.textContent=(file.diff_note?file.diff_note+' · ':'')+'Kernel: '+file.kernel;
  notebook.appendChild(banner);
  file.cells.forEach(cell=>{
    const article=document.createElement('article');
    article.className='notebook-cell cell-'+cell.type;
    const head=document.createElement('div');
    head.className='cell-head';
    const type=document.createElement('strong');
    type.textContent=cell.type+' cell '+(cell.index+1);
    head.appendChild(type);
    if(cell.execution_count!=null) {
      const execution=document.createElement('span');
      execution.textContent='['+cell.execution_count+']';
      head.appendChild(execution);
    }
    article.appendChild(head);

    if(cell.type==='markdown') {
      const rendered=document.createElement('div');
      rendered.className='markdown-body';
      rendered.innerHTML=cell.rendered_html||'';
      article.appendChild(rendered);
      if(cell.lines.length) {
        const details=document.createElement('details');
        details.className='cell-source-details';
        const summary=document.createElement('summary');
        summary.textContent='Review Markdown source';
        details.open=cell.lines.some(line=>comments.get(line.id)?.trim());
        details.appendChild(summary);
        const source=document.createElement('div');
        source.className='cell-source';
        cell.lines.forEach(line=>appendReviewLine(source,file,line));
        details.appendChild(source);
        article.appendChild(details);
      }
    } else {
      const source=document.createElement('div');
      source.className='cell-source';
      if(cell.lines.length) cell.lines.forEach(line=>appendReviewLine(source,file,line));
      else {
        const empty=document.createElement('div');
        empty.className='cell-empty';
        empty.textContent='Empty cell';
        source.appendChild(empty);
      }
      article.appendChild(source);
    }

    if(cell.outputs?.length) {
      const outputs=document.createElement('div');
      outputs.className='cell-outputs';
      cell.outputs.forEach(output=>outputs.appendChild(renderNotebookOutput(output)));
      article.appendChild(outputs);
    }
    notebook.appendChild(article);
  });
  code.appendChild(notebook);
}

function renderCode() {
  hideHover();
  code.replaceChildren();
  code.scrollTop=0;
  code.scrollLeft=0;
  if(!data.files.length) {
    const empty=document.createElement('div');
    empty.className='empty';
    empty.textContent='No reviewable changes found.';
    code.appendChild(empty);
    return;
  }
  const file=data.files[active];
  if(file.notebook) renderNotebook(file);
  else file.lines.forEach(line=>appendReviewLine(code,file,line));
  requestAnimationFrame(updateHorizontalScroll);
}

function hideHover() {
  if(hoverTimer) clearTimeout(hoverTimer);
  hoverTimer=null;
  hoverRequest+=1;
  hover.hidden=true;
}

function placeHover(x,y) {
  hover.hidden=false;
  const margin=12;
  const width=hover.offsetWidth;
  const height=hover.offsetHeight;
  hover.style.left=Math.max(margin,Math.min(x+12,window.innerWidth-width-margin))+'px';
  hover.style.top=Math.max(margin,Math.min(y+18,window.innerHeight-height-margin))+'px';
}

async function requestHover(target,x,y) {
  const row=target.closest('.row');
  if(!row?.dataset.newLine) return;
  const request=++hoverRequest;
  try {
    const response=await fetch('/hover',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        token:sessionToken,
        file:row.dataset.file,
        line:Number(row.dataset.newLine),
        column:Number(target.dataset.column)
      })
    });
    const result=await response.json();
    if(request!==hoverRequest) return;
    hover.textContent=result.text||'No documentation available.';
    placeHover(x,y);
  } catch(error) {
    if(request!==hoverRequest) return;
    hover.textContent='Hover unavailable: '+error.message;
    placeHover(x,y);
  }
}

code.addEventListener('pointerover',event=>{
  const target=event.target.closest('.tok[data-column]');
  if(!target) return;
  if(hoverTimer) clearTimeout(hoverTimer);
  const x=event.clientX, y=event.clientY;
  hoverTimer=setTimeout(()=>requestHover(target,x,y),350);
});
code.addEventListener('pointerleave',hideHover);
code.addEventListener('scroll',()=>{
  hideHover();
  if(hscroll.scrollLeft!==code.scrollLeft) hscroll.scrollLeft=code.scrollLeft;
},{passive:true});
hscroll.addEventListener('scroll',()=>{
  if(code.scrollLeft!==hscroll.scrollLeft) code.scrollLeft=hscroll.scrollLeft;
},{passive:true});
code.addEventListener('wheel',event=>{
  if(event.shiftKey && event.deltaY) {
    code.scrollLeft+=event.deltaY;
    event.preventDefault();
  }
},{passive:false});
new ResizeObserver(updateHorizontalScroll).observe(code);
document.getElementById('font-minus').onclick=()=>setCodeFontSize(codeFontSize-1);
document.getElementById('font-plus').onclick=()=>setCodeFontSize(codeFontSize+1);
document.addEventListener('keydown',event=>{ if(event.key==='Escape' && !event.target.matches('textarea')) hideHover(); });

async function finish(status) {
  const output=[];
  data.files.forEach(file=>file.lines.forEach(line=>{
    const comment=(comments.get(line.id)||'').trim();
    if(comment) output.push({
      file:file.path,
      old_line:line.old_line,
      new_line:line.new_line,
      kind:line.kind,
      line_text:line.text,
      cell_index:line.cell_index??null,
      cell_line:line.cell_line??null,
      cell_type:line.cell_type??null,
      comment
    });
  }));
  const body={
    token:sessionToken,
    status,
    mode:data.mode,
    root:data.root,
    scope:data.scope,
    comments:output,
    overall:document.getElementById('overall').value.trim()
  };
  const send=document.getElementById('send');
  const cancel=document.getElementById('cancel');
  send.disabled=true;
  cancel.disabled=true;
  try {
    const response=await fetch('/submit',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)
    });
    if(!response.ok) throw new Error('submission failed');
    document.body.innerHTML='<div class="empty">Review sent. You can close this tab.</div>';
  } catch(error) {
    send.disabled=false;
    cancel.disabled=false;
    window.alert('Could not send review: '+error.message);
  }
}

document.getElementById('send').onclick=()=>finish('submitted');
document.getElementById('cancel').onclick=()=>finish('cancelled');
document.addEventListener('keydown',event=>{
  if((event.metaKey||event.ctrlKey)&&event.key==='Enter') finish('submitted');
});
setCodeFontSize(codeFontSize);
renderFiles();
renderCode();
updateCount();
