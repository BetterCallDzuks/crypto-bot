// PM2 process definition for Kovanica Bot.
//
// Usage:
//   ./setup.sh                       # one-time: create venv + install deps
//   pm2 start ecosystem.config.js    # start the bot + dashboard under PM2
//   pm2 logs kovanica-bot              # follow logs
//   pm2 restart kovanica-bot           # restart (needed after symbol/quote change)
//   pm2 stop kovanica-bot              # stop
//   pm2 save && pm2 startup          # keep it running across reboots
//
// PM2 runs the Python entrypoint via the project virtualenv interpreter.

module.exports = {
  apps: [
    {
      name: "kovanica-bot",
      script: "run.py",
      interpreter: "./.venv/bin/python",
      cwd: __dirname,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      // Unbuffered output so `pm2 logs` shows lines immediately.
      env: { PYTHONUNBUFFERED: "1" },
      out_file: "./data/pm2-out.log",
      error_file: "./data/pm2-error.log",
      time: true,
    },
  ],
};
