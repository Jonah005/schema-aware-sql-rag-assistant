# Schema-Aware SQL RAG Assistant

A database question-answering system that lets users ask natural-language questions about structured business data.

The application uses **Qdrant** and vector-based semantic search to retrieve the most relevant database/schema information for a user’s question. The retrieved context is then passed to an **LLM**, which uses that information together with conversation history stored in **Redis** to generate a grounded response.

## What It Solves

Business users often need information from databases but may not know SQL or understand the underlying database structure.

This project provides a simpler interface where users can ask questions in natural language and receive answers based on relevant database information.

## How It Works

1. The user asks a question through the chat interface.
2. The system searches for the most relevant database/schema information using **Qdrant** and vector similarity.
3. The top matching results are provided as context to the LLM.
4. Conversation history is retrieved from **Redis** so follow-up questions can retain context.
5. The LLM uses the retrieved database context and chat history to generate the response.

## Key Features

- Natural-language database querying
- RAG-based retrieval using Qdrant
- Vector-based semantic matching
- Top-K retrieval of relevant database/schema information
- LLM-powered response generation
- Redis-based conversation history
- Support for contextual follow-up questions

## Tech Stack

- Python
- Django
- PostgreSQL
- Qdrant
- Redis
- Large Language Models
- Sentence Transformers
- Docker

## Goal

The goal of the project is to make structured business data easier to access by allowing users to interact with databases through natural-language questions instead of manually writing SQL queries.
