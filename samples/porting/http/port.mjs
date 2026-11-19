import { createServer } from 'node:http';

const defect = process.argv.includes('--defect');
const rates = Array.from({length: 101}, (_, index) => 100 + 15 * index);

function reply(response, status, payload) {
  const text = JSON.stringify(payload);
  response.writeHead(status, {'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(text)});
  response.end(text);
}

createServer(async (request, response) => {
  const route = new URL(request.url, 'http://127.0.0.1');
  if (request.method === 'GET' && route.pathname === '/status') {
    response.writeHead(204).end();
    return;
  }
  if (request.method !== 'POST' || route.pathname !== '/price' ||
      route.searchParams.getAll('unit').join(',') !== 'kg') {
    reply(response, 404, {error: 'route'});
    return;
  }
  let value;
  try {
    const chunks = [];
    for await (const bytes of request) chunks.push(bytes);
    value = JSON.parse(Buffer.concat(chunks)).weight;
  } catch {
    value = null;
  }
  const digits = typeof value === 'string' && value.length > 0 && value.length <= 3 &&
    [...value].every(character => character >= '0' && character <= '9');
  const amount = digits ? rates[Number(value)] : undefined;
  if (amount === undefined) {
    reply(response, 422, {error: 'weight'});
  } else {
    reply(response, 200, {amount: defect && Number(value) === 100 ? amount - 1 : amount});
  }
}).listen(Number(process.env.RECHECK_PORT), '127.0.0.1');
