"""Optional published example-vector routing, independent of memory relevance."""

import json
from pathlib import Path

from .index import unit_vector


def validate_routes(data, profile, dimension):
    """Offline validation includes boundaries even when a query would skip them."""
    if (data.get('format') != 'serein-query-routes-v1' or data.get('profile') != profile
            or data.get('dimension') != dimension):
        raise ValueError('Query routes must match the current embedding profile and dimension')
    policy=data['policy']
    if type(policy['aggregation_top_k']) is not int or policy['aggregation_top_k'] < 1:
        raise ValueError('Query route aggregation_top_k must be positive')
    for key in ('min_score','min_margin','boundary_veto_min_score','boundary_veto_max_deficit'):
        if type(policy[key]) not in (int,float) or not 0 <= policy[key] <= 1:
            raise ValueError('Invalid query route policy: '+key)
    if type(policy['boundary_veto_enabled']) is not bool:
        raise ValueError('Invalid query route boundary switch')
    if not isinstance(data['routes'],list) or not data['routes']:
        raise ValueError('Query routes are empty')
    for route in data['routes']:
        if not route['name'] or route['action'] not in ('recall','skip') or not route['vectors']:
            raise ValueError('Invalid query route')
        if route['threshold'] is not None and not 0 <= route['threshold'] <= 1:
            raise ValueError('Invalid query route threshold')
        for vector in route['vectors']:unit_vector(vector,dimension)
    for boundary in data['boundaries']:
        if boundary['action'] not in ('recall','skip'):raise ValueError('Invalid query route boundary')
        unit_vector(boundary['vector'],dimension)
    if not data['generation']:raise ValueError('Query route generation is missing')


def route_query(path, embedding, *, data=None):
    data = data if data is not None else json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("format") != "serein-query-routes-v1" or data["profile"] != embedding["profile"]:
        raise ValueError("Query routes must match the current embedding profile")
    vector = unit_vector(embedding["embedding"], data["dimension"])
    policy = data["policy"]

    def similarity(other):
        return sum(a * b for a, b in zip(vector, unit_vector(other, data["dimension"])))

    scores = []
    for route in data["routes"]:
        values = sorted((similarity(row) for row in route["vectors"]), reverse=True)[:policy["aggregation_top_k"]]
        if values:
            scores.append({"name": route["name"], "action": route["action"],
                           "score": sum(values) / len(values),
                           "threshold": route["threshold"] if route["threshold"] is not None else policy["min_score"]})
    scores.sort(key=lambda row: (-row["score"], row["name"]))
    result = {"action": "recall", "reason": "no_route", "generation": data["generation"], "scores": scores}
    if not scores:
        return result
    winner = scores[0]
    opposite = max((row["score"] for row in scores if row["action"] != winner["action"]), default=0)
    result.update(route=winner["name"], score=winner["score"], margin=winner["score"]-opposite)
    if winner["action"] != "skip":
        return {**result, "reason": "recall_route"}
    if winner["score"] < winner["threshold"] or result["margin"] < policy["min_margin"]:
        return {**result, "reason": "uncertain_route"}
    if policy["boundary_veto_enabled"]:
        boundaries = [similarity(row["vector"]) for row in data["boundaries"] if row["action"] != "skip"]
        if boundaries:
            best = max(boundaries)
            if best >= policy["boundary_veto_min_score"] and max(0, winner["score"]-best) <= policy["boundary_veto_max_deficit"]:
                return {**result, "reason": "recall_boundary", "boundary_score": best}
    return {**result, "action": "skip", "reason": "published_skip_route"}
