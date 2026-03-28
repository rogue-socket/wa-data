const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');

const BACKEND_URL = 'http://localhost:8000/ingest';

const client = new Client({
  authStrategy: new LocalAuth(),
});

client.on('qr', (qr) => {
  qrcode.generate(qr, { small: true });
  console.log('Scan the QR code above to connect WhatsApp Web.');
});

client.on('ready', () => {
  console.log('WhatsApp client is ready.');
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

  const payload = {
    text: msg.body,
    sender: msg.author || msg.from,
    group_id: msg.from,
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

client.initialize();
