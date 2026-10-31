FROM node:20-slim

WORKDIR /app

COPY package.json .
RUN npm install --omit=dev

COPY . .

# Local audit outbox (mounted as a named volume in docker-compose.yml).
# The directory is created here so that the volume inherits its owner.
RUN mkdir -p /var/lib/fhir-middleware && chown node:node /var/lib/fhir-middleware

# Run without root privileges.
USER node

EXPOSE 3000 2575

CMD ["node", "src/index.js"]
