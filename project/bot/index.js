const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');

const BACKEND_URL = process.env.BACKEND_URL || 'http://127.0.0.1:8000/ingest';
const WA_CLIENT_ID = process.env.WA_CLIENT_ID || 'wa-data-bot';
const MAX_INIT_RETRIES = Number.parseInt(process.env.WA_INIT_RETRIES || '5', 10);

const NODE_MAJOR = Number.parseInt(process.versions.node.split('.')[0], 10);
if (NODE_MAJOR >= 25) {
  console.warn('Node 25 is non-LTS and can be unstable with whatsapp-web.js. Prefer Node 24/22 LTS.');
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isExecutionContextDestroyed(error) {
  const message = error instanceof Error ? error.message : String(error || '');
  return message.includes('Execution context was destroyed');
}

const client = new Client({
  authStrategy: new LocalAuth({ clientId: WA_CLIENT_ID }),
  authTimeoutMs: 60000,
  puppeteer: {
    headless: true,
    args: [
      '--disable-setuid-sandbox',
      '--disable-dev-shm-usage',
    ],
  },
});

client.on('qr', (qr) => {
  qrcode.generate(qr, { small: true });
  console.log('Scan the QR code above to connect WhatsApp Web.');
});

client.on('ready', () => {
  console.log('WhatsApp client is ready.');
});

client.on('auth_failure', (message) => {
  console.error('WhatsApp auth failure:', message);
});

client.on('disconnected', (reason) => {
  console.warn('WhatsApp client disconnected:', reason);
});

client.on('message', async (msg) => {
  console.log('Received message:', {
    from: msg.from,
    author: msg.author,
    body: msg.body,
    timestamp: msg.timestamp,
  });

  if (!msg.from || !msg.from.includes('@g.us')) {
    return;
  }

  let groupName = msg.from;
  try {
    const chat = await msg.getChat();
    if (chat && chat.name) {
      groupName = chat.name;
    }
  } catch (error) {
    console.warn('Unable to resolve group name. Falling back to group_id.', error.message);
  }

  const payload = {
    text: msg.body,
    sender: msg.author || msg.from,
    group_id: msg.from,
    group_name: groupName,
    timestamp: msg.timestamp,
  };

  try {
    await axios.post(BACKEND_URL, payload);
    console.log('API success: message stored in backend.');
  } catch (error) {
    const status = error.response ? error.response.status : 'no_response';
    const details = error.response ? error.response.data : error.message;
    console.error('API failure while storing message:', { status, details });
  }
});

async function initializeClientWithRetry() {
  for (let attempt = 1; attempt <= MAX_INIT_RETRIES; attempt += 1) {
    try {
      console.log(`Initializing WhatsApp client (attempt ${attempt}/${MAX_INIT_RETRIES})...`);
      await client.initialize();
      return;
    } catch (error) {
      if (isExecutionContextDestroyed(error) && attempt < MAX_INIT_RETRIES) {
        const backoffMs = Math.min(1000 * (2 ** (attempt - 1)), 10000);
        console.warn(`Transient browser context error. Retrying in ${backoffMs}ms...`);
        await sleep(backoffMs);
        continue;
      }

      console.error('Failed to initialize WhatsApp client.');
      console.error(error);
      console.error('If this keeps happening, clear local auth state with: rm -rf .wwebjs_auth');
      process.exit(1);
    }
  }
}

initializeClientWithRetry();
