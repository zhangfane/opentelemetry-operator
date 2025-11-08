# Get CA certificates from alpine package repo
FROM alpine:3.22 AS certificates

RUN apk --no-cache add ca-certificates

######## Start a new stage from scratch #######
FROM golang:1.25
RUN go install github.com/go-delve/delve/cmd/dlv@latest

ARG TARGETARCH

WORKDIR /
ADD ./ ./
RUN go build -o manager main.go

# Copy the certs from Alpine
COPY --from=certificates /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt


USER 65532:65532

ENTRYPOINT ["dlv","--listen=:40000","--headless=true","--api-version=2","--accept-multiclient","exec","--","/manager"]
