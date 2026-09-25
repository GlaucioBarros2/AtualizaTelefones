#!/usr/bin/env python3
"""
Atualiza Telefones
==================

Lê o cadastro de clientes (SACADCLI.DBF), consulta cada CNPJ (campo CGCCLI)
em APIs públicas com dados da Receita Federal e gera um CSV com os telefones
encontrados:

    CODCLI;CNPJ;Telefone1;Telefone2;Telefone3...

APIs utilizadas (em ordem; se uma falhar, tenta a próxima):
    1. BrasilAPI      - https://brasilapi.com.br/api/cnpj/v1/{cnpj}
    2. Minha Receita  - https://minhareceita.org/{cnpj}
    3. CNPJ.ws        - https://publica.cnpj.ws/cnpj/{cnpj}   (3 consultas/min)
    4. ReceitaWS      - https://receitaws.com.br/v1/cnpj/{cnpj} (3 consultas/min)

Usa apenas a biblioteca padrão do Python (3.8+), sem dependências externas.

As respostas ficam gravadas em um arquivo de cache (JSON). Se o programa for
interrompido, basta executá-lo novamente: os CNPJs já consultados não são
pesquisados de novo.

Exemplo:
    python atualiza_telefones.py SACADCLI.DBF -o telefones.csv
"""

import argparse
import csv
import datetime
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request

USER_AGENT = "AtualizaTelefones/1.0 (+consulta de CNPJ)"


# ---------------------------------------------------------------------------
# Leitura do DBF (dBase III / Clipper)
# ---------------------------------------------------------------------------

def ler_dbf(caminho, encoding="cp850"):
    """Gera um dict por registro não excluído do arquivo DBF."""
    with open(caminho, "rb") as f:
        cabecalho = f.read(32)
        if len(cabecalho) < 32:
            raise ValueError("Arquivo DBF inválido: cabeçalho incompleto")
        num_registros, tam_cabecalho, tam_registro = struct.unpack("<IHH", cabecalho[4:12])

        campos = []
        deslocamento = 1  # byte 0 de cada registro é a marca de exclusão
        while True:
            desc = f.read(32)
            if not desc or desc[0] == 0x0D:
                break
            nome = desc[:11].split(b"\x00")[0].decode("ascii", "replace").strip()
            tipo = chr(desc[11])
            tamanho = desc[16]
            campos.append((nome, tipo, deslocamento, tamanho))
            deslocamento += tamanho

        f.seek(tam_cabecalho)
        for _ in range(num_registros):
            reg = f.read(tam_registro)
            if len(reg) < tam_registro or reg[:1] == b"\x1a":
                break
            if reg[:1] == b"*":  # registro excluído
                continue
            yield {
                nome: reg[ini:ini + tam].decode(encoding, "replace").strip()
                for nome, _tipo, ini, tam in campos
            }


# ---------------------------------------------------------------------------
# CNPJ
# ---------------------------------------------------------------------------

def _dv_cnpj(base):
    def digito(numeros, pesos):
        soma = sum(int(n) * p for n, p in zip(numeros, pesos))
        resto = soma % 11
        return "0" if resto < 2 else str(11 - resto)

    d1 = digito(base, [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    d2 = digito(base + d1, [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2])
    return d1 + d2


def cnpj_valido(cnpj):
    return (
        len(cnpj) == 14
        and cnpj.isdigit()
        and cnpj != cnpj[0] * 14
        and _dv_cnpj(cnpj[:12]) == cnpj[12:]
    )


def normalizar_cnpj(valor):
    """Remove pontuação e completa zeros à esquerda perdidos. Retorna '' se inválido."""
    digitos = re.sub(r"\D", "", valor or "")
    if not digitos or len(digitos) > 14:
        return ""
    digitos = digitos.zfill(14)
    return digitos if cnpj_valido(digitos) else ""


# ---------------------------------------------------------------------------
# Telefones
# ---------------------------------------------------------------------------

def normalizar_telefone(valor):
    """Converte um telefone com DDD para somente dígitos (ex.: '8132044000')."""
    digitos = re.sub(r"\D", "", valor or "")
    digitos = digitos.lstrip("0")          # ex.: '081...' ou '0xx81...'
    if digitos.startswith("55") and len(digitos) in (12, 13):
        digitos = digitos[2:]               # remove código do país
    if len(digitos) not in (10, 11):        # DDD + 8 ou 9 dígitos
        return ""
    if digitos.strip("0") == "" or len(set(digitos[2:])) == 1:
        return ""
    return digitos


def formatar_telefone(digitos):
    ddd, numero = digitos[:2], digitos[2:]
    return "({}) {}-{}".format(ddd, numero[:-4], numero[-4:])


# ---------------------------------------------------------------------------
# Provedores de consulta
# ---------------------------------------------------------------------------

class ErroConsulta(Exception):
    pass


class CnpjNaoEncontrado(Exception):
    pass


class LimiteExcedido(Exception):
    pass


def http_get_json(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise CnpjNaoEncontrado("HTTP 404")
        if e.code == 429:
            raise LimiteExcedido("HTTP 429")
        raise ErroConsulta("HTTP {}".format(e.code))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise ErroConsulta(str(e))


class Provedor:
    nome = ""
    url = ""
    intervalo = 0.0  # segundos mínimos entre consultas

    def __init__(self):
        self._ultima = 0.0
        self.bloqueado_ate = 0.0

    def disponivel(self):
        return time.monotonic() >= self.bloqueado_ate

    def consultar(self, cnpj, timeout):
        espera = self.intervalo - (time.monotonic() - self._ultima)
        if espera > 0:
            time.sleep(espera)
        self._ultima = time.monotonic()
        dados = http_get_json(self.url.format(cnpj=cnpj), timeout)
        return self.extrair_telefones(dados)

    def extrair_telefones(self, dados):
        raise NotImplementedError


class BrasilAPI(Provedor):
    nome = "BrasilAPI"
    url = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
    intervalo = 0.5

    def extrair_telefones(self, d):
        return [d.get("ddd_telefone_1"), d.get("ddd_telefone_2"), d.get("ddd_fax")]


class MinhaReceita(Provedor):
    nome = "MinhaReceita"
    url = "https://minhareceita.org/{cnpj}"
    intervalo = 0.5

    def extrair_telefones(self, d):
        return [d.get("ddd_telefone_1"), d.get("ddd_telefone_2"), d.get("ddd_fax")]


class CnpjWs(Provedor):
    nome = "CNPJ.ws"
    url = "https://publica.cnpj.ws/cnpj/{cnpj}"
    intervalo = 21.0

    def extrair_telefones(self, d):
        e = d.get("estabelecimento") or {}
        pares = [("ddd1", "telefone1"), ("ddd2", "telefone2"), ("ddd_fax", "fax")]
        return ["{}{}".format(e.get(a) or "", e.get(b) or "") if e.get(b) else "" for a, b in pares]


class ReceitaWS(Provedor):
    nome = "ReceitaWS"
    url = "https://receitaws.com.br/v1/cnpj/{cnpj}"
    intervalo = 21.0

    def extrair_telefones(self, d):
        if d.get("status") == "ERROR":
            msg = d.get("message", "")
            if "inválido" in msg.lower() or "não encontrado" in msg.lower():
                raise CnpjNaoEncontrado(msg)
            raise ErroConsulta(msg)
        return re.split(r"[/,;]", d.get("telefone") or "")


PROVEDORES = {p.nome.lower(): p for p in (BrasilAPI, MinhaReceita, CnpjWs, ReceitaWS)}


def consultar_cnpj(cnpj, provedores, timeout, tentativas):
    """
    Retorna (lista_de_telefones, nome_do_provedor).
    Levanta CnpjNaoEncontrado ou ErroConsulta se nenhum provedor responder.
    """
    erros = []
    nao_encontrado = 0
    for rodada in range(tentativas):
        for p in provedores:
            if not p.disponivel():
                continue
            try:
                brutos = p.consultar(cnpj, timeout)
            except CnpjNaoEncontrado as e:
                nao_encontrado += 1
                erros.append("{}: não encontrado".format(p.nome))
                continue
            except LimiteExcedido as e:
                p.bloqueado_ate = time.monotonic() + 60
                erros.append("{}: {}".format(p.nome, e))
                continue
            except ErroConsulta as e:
                erros.append("{}: {}".format(p.nome, e))
                continue

            telefones = []
            for t in brutos:
                n = normalizar_telefone(t)
                if n and n not in telefones:
                    telefones.append(n)
            return telefones, p.nome

        if nao_encontrado:
            break
        # Todos falharam/estão bloqueados: aguarda antes de nova rodada.
        time.sleep(min(60, 5 * 2 ** rodada))

    if nao_encontrado:
        raise CnpjNaoEncontrado("; ".join(erros))
    raise ErroConsulta("; ".join(erros) or "nenhum provedor disponível")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def carregar_cache(caminho):
    if caminho and os.path.exists(caminho):
        with open(caminho, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def salvar_cache(caminho, cache):
    if not caminho:
        return
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=0)
    os.replace(tmp, caminho)


# ---------------------------------------------------------------------------
# Programa principal
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Consulta os CNPJs (CGCCLI) de um DBF de clientes e gera CSV com os telefones."
    )
    ap.add_argument("dbf", help="arquivo DBF de clientes (ex.: SACADCLI.DBF)")
    ap.add_argument("-o", "--saida", default="telefones.csv", help="CSV de saída (padrão: telefones.csv)")
    ap.add_argument("--cache", default=None,
                    help="arquivo de cache JSON (padrão: <saida>.cache.json). Use '' para desativar")
    ap.add_argument("--log", default=None, help="CSV com CNPJs inválidos/não encontrados (padrão: <saida>.erros.csv)")
    ap.add_argument("--provedores", default="brasilapi,minhareceita,cnpj.ws,receitaws",
                    help="ordem dos provedores, separados por vírgula (padrão: %(default)s)")
    ap.add_argument("--apenas-digitos", action="store_true",
                    help="grava telefones só com dígitos (8132044000) em vez de (81) 3204-4000")
    ap.add_argument("--incluir-sem-telefone", action="store_true",
                    help="inclui no CSV clientes cujo CNPJ não retornou telefone")
    ap.add_argument("--limite", type=int, default=0, help="processa apenas os N primeiros registros (teste)")
    ap.add_argument("--timeout", type=float, default=20, help="timeout de cada requisição em segundos")
    ap.add_argument("--tentativas", type=int, default=3, help="rodadas de tentativas por CNPJ")
    ap.add_argument("--reconsultar-erros", action="store_true",
                    help="consulta de novo CNPJs que deram erro de comunicação em execuções anteriores")
    ap.add_argument("--encoding-dbf", default="cp850", help="codificação do DBF (padrão: cp850)")
    args = ap.parse_args(argv)

    cache_path = args.cache if args.cache is not None else args.saida + ".cache.json"
    log_path = args.log or os.path.splitext(args.saida)[0] + ".erros.csv"

    provedores = []
    for nome in args.provedores.split(","):
        nome = nome.strip().lower()
        if nome not in PROVEDORES:
            ap.error("provedor desconhecido: {} (opções: {})".format(nome, ", ".join(PROVEDORES)))
        provedores.append(PROVEDORES[nome]())

    # 1. Lê os registros do DBF
    clientes = []
    for i, reg in enumerate(ler_dbf(args.dbf, args.encoding_dbf)):
        if args.limite and i >= args.limite:
            break
        clientes.append((reg.get("CODCLI", ""), reg.get("CGCCLI", "")))
    print("Registros lidos do DBF: {}".format(len(clientes)))

    # 2. Consulta os CNPJs únicos e válidos
    cache = carregar_cache(cache_path)
    unicos = list(dict.fromkeys(filter(None, (normalizar_cnpj(cgc) for _cod, cgc in clientes))))

    pendentes = [
        c for c in unicos
        if c not in cache or (args.reconsultar_erros and cache[c].get("status") == "erro")
    ]
    print("CNPJs válidos distintos: {} | já no cache: {} | a consultar: {}".format(
        len(unicos), len(unicos) - len(pendentes), len(pendentes)))

    inicio = time.monotonic()
    try:
        for n, cnpj in enumerate(pendentes, 1):
            agora = datetime.datetime.now().isoformat(timespec="seconds")
            try:
                telefones, fonte = consultar_cnpj(cnpj, provedores, args.timeout, args.tentativas)
                cache[cnpj] = {"status": "ok", "telefones": telefones, "fonte": fonte, "data": agora}
                resumo = ", ".join(telefones) or "(sem telefone)"
            except CnpjNaoEncontrado as e:
                cache[cnpj] = {"status": "nao_encontrado", "telefones": [], "erro": str(e), "data": agora}
                resumo = "NÃO ENCONTRADO"
            except ErroConsulta as e:
                cache[cnpj] = {"status": "erro", "telefones": [], "erro": str(e), "data": agora}
                resumo = "ERRO: {}".format(e)

            decorrido = time.monotonic() - inicio
            restante = decorrido / n * (len(pendentes) - n)
            print("[{}/{}] {} -> {}  (restam ~{:.0f} min)".format(
                n, len(pendentes), cnpj, resumo, restante / 60), flush=True)

            if n % 10 == 0:
                salvar_cache(cache_path, cache)
    except KeyboardInterrupt:
        print("\nInterrompido pelo usuário. Progresso salvo; execute novamente para continuar.")
    finally:
        salvar_cache(cache_path, cache)

    # 3. Gera o CSV
    fmt = (lambda t: t) if args.apenas_digitos else formatar_telefone
    linhas, erros = [], []
    for cod, cgc in clientes:
        cnpj = normalizar_cnpj(cgc)
        if not cnpj:
            erros.append((cod, cgc, "CNPJ vazio ou inválido"))
            continue
        info = cache.get(cnpj)
        if info is None:
            erros.append((cod, cnpj, "não consultado (execução interrompida)"))
            continue
        if info["status"] != "ok":
            erros.append((cod, cnpj, "{}: {}".format(info["status"], info.get("erro", ""))))
            continue
        if info["telefones"] or args.incluir_sem_telefone:
            linhas.append([cod, cnpj] + [fmt(t) for t in info["telefones"]])
        else:
            erros.append((cod, cnpj, "sem telefone na Receita"))

    max_tel = max([len(l) - 2 for l in linhas] + [1])
    cabecalho = ["CODCLI", "CNPJ"] + ["Telefone{}".format(i) for i in range(1, max_tel + 1)]

    with open(args.saida, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cabecalho)
        for l in linhas:
            w.writerow(l + [""] * (len(cabecalho) - len(l)))

    with open(log_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["CODCLI", "CGCCLI", "Motivo"])
        w.writerows(erros)

    print("\nCSV gerado: {} ({} clientes com telefone)".format(args.saida, len(linhas)))
    print("Pendências: {} ({} registros)".format(log_path, len(erros)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
