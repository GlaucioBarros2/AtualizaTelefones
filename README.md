# AtualizaTelefones

Lê o cadastro de clientes (`SACADCLI.DBF`), consulta cada CNPJ (campo `CGCCLI`) em
APIs públicas com dados da Receita Federal e gera um CSV com os telefones:

```
CODCLI;CNPJ;Telefone1;Telefone2;Telefone3
002218;01245580000140;(81) 3204-4000;(81) 99969-8130;
```

## Requisitos

Python 3.8 ou mais novo. Não precisa instalar nenhuma biblioteca.

## Uso

```
python atualiza_telefones.py SACADCLI.DBF -o telefones.csv
```

Arquivos gerados:

| Arquivo | Conteúdo |
|---|---|
| `telefones.csv` | `CODCLI;CNPJ;Telefone1;Telefone2;...` (separado por `;`, UTF-8, abre direto no Excel) |
| `telefones.erros.csv` | clientes sem CNPJ válido, CNPJ não encontrado, sem telefone ou com erro de consulta |
| `telefones.csv.cache.json` | respostas já obtidas. Se o programa parar, rode de novo e ele continua de onde parou |

Opções úteis:

- `--limite 20`: processa só os 20 primeiros registros (para testar)
- `--apenas-digitos`: grava `8132044000` em vez de `(81) 3204-4000`
- `--incluir-sem-telefone`: inclui no CSV clientes cujo CNPJ não tem telefone na Receita
- `--reconsultar-erros`: consulta de novo os CNPJs que falharam por erro de rede ou de limite
- `--provedores brasilapi,minhareceita`: escolhe quais APIs usar e em que ordem

## APIs usadas

Todas são gratuitas e sem chave. Se uma falhar, o programa tenta a próxima:

1. [BrasilAPI](https://brasilapi.com.br/docs#tag/CNPJ)
2. [Minha Receita](https://minhareceita.org)
3. [CNPJ.ws](https://www.cnpj.ws/docs/api-publica) (3 consultas por minuto)
4. [ReceitaWS](https://developers.receitaws.com.br) (3 consultas por minuto)

A Receita Federal guarda no máximo 2 telefones e 1 fax por CNPJ, então o CSV
terá até 3 colunas de telefone.

## Tratamento dos dados

- Registros marcados como excluídos no DBF são ignorados.
- O CNPJ é validado pelos dígitos verificadores. Zeros à esquerda que se perderam são completados.
- Quando vários clientes têm o mesmo CNPJ, ele é consultado só uma vez.
- Telefones repetidos (por exemplo, fax igual ao telefone) aparecem uma vez só.
