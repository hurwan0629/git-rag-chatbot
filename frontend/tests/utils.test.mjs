import test from 'node:test';
import assert from 'node:assert/strict';
import {unicodeSlice,safeGithubUrl,progressLabel} from '../src/utils.mjs';
test('Python character offsets preserve emoji and CRLF',()=>{
 const text='가😀\r\n끝';assert.equal(unicodeSlice(text,1,4),'😀\r\n');
});
test('source links allow only canonical HTTPS GitHub',()=>{
 assert.ok(safeGithubUrl('https://github.com/a/b/blob/abc/README.md'));
 for(const url of ['javascript:alert(1)','https://github.com.evil/a','http://github.com/a','https://user@github.com/a','https://github.com:8080/a'])assert.equal(safeGithubUrl(url),null);
});
test('unknown and zero totals do not claim 100 percent',()=>{
 assert.equal(progressLabel({unit:null,current:0,total:null,percent:null}),'총량 확인 중');
 assert.equal(progressLabel({unit:'chunks',current:0,total:0,percent:null}),'0 / 0 chunks');
 assert.equal(progressLabel({unit:'files',current:5,total:10,percent:50}),'5 / 10 files · 50.0%');
});
