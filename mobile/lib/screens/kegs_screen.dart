import 'package:flutter/material.dart';

import '../models/keg.dart';
import '../services/api_service.dart';

class KegsScreen extends StatefulWidget {
  final ApiService api;

  const KegsScreen({super.key, required this.api});

  @override
  State<KegsScreen> createState() => _KegsScreenState();
}

class _KegsScreenState extends State<KegsScreen> {
  late Future<List<Keg>> _future;

  @override
  void initState() {
    super.initState();
    _future = widget.api.fetchKegs();
  }

  void _refresh() => setState(() => _future = widget.api.fetchKegs());

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<List<Keg>>(
      future: _future,
      builder: (context, snapshot) {
        if (snapshot.connectionState == ConnectionState.waiting) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snapshot.hasError) {
          return _ErrorRetry(message: snapshot.error.toString(), onRetry: _refresh);
        }

        final kegs = snapshot.requireData;

        if (kegs.isEmpty) {
          return Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Icon(Icons.sports_bar, size: 48, color: Colors.grey),
                const SizedBox(height: 12),
                const Text('No kegs found', style: TextStyle(color: Colors.grey)),
                const SizedBox(height: 16),
                OutlinedButton.icon(onPressed: _refresh, icon: const Icon(Icons.refresh), label: const Text('Refresh')),
              ],
            ),
          );
        }

        return RefreshIndicator(
          onRefresh: () async => _refresh(),
          child: ListView.separated(
            padding: const EdgeInsets.all(12),
            itemCount: kegs.length,
            separatorBuilder: (_, __) => const SizedBox(height: 8),
            itemBuilder: (context, index) {
              final keg = kegs[index];
              return Card(
                child: Padding(
                  padding: const EdgeInsets.all(12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(
                        children: [
                          Expanded(
                            child: Text(keg.name, style: Theme.of(context).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.bold)),
                          ),
                          Chip(
                            label: Text(keg.status.replaceAll('_', ' ')),
                            backgroundColor: keg.statusColor.withOpacity(0.15),
                            side: BorderSide(color: keg.statusColor),
                            labelStyle: TextStyle(color: keg.statusColor, fontSize: 12),
                          ),
                        ],
                      ),
                      if (keg.brewery.isNotEmpty) ...[
                        const SizedBox(height: 4),
                        Text(keg.brewery, style: Theme.of(context).textTheme.bodyMedium),
                      ],
                      const SizedBox(height: 8),
                      Wrap(
                        spacing: 8,
                        children: [
                          if (keg.type.isNotEmpty) _InfoChip(label: keg.type),
                          if (keg.size.isNotEmpty) _InfoChip(label: keg.size),
                          if (keg.abv.isNotEmpty) _InfoChip(label: '${keg.abv}% ABV'),
                        ],
                      ),
                      if (keg.notes.isNotEmpty) ...[
                        const SizedBox(height: 8),
                        Text(keg.notes, style: Theme.of(context).textTheme.bodySmall?.copyWith(color: Colors.grey)),
                      ],
                    ],
                  ),
                ),
              );
            },
          ),
        );
      },
    );
  }
}

class _InfoChip extends StatelessWidget {
  final String label;
  const _InfoChip({required this.label});

  @override
  Widget build(BuildContext context) {
    return Chip(
      label: Text(label, style: const TextStyle(fontSize: 12)),
      materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
      padding: EdgeInsets.zero,
      visualDensity: VisualDensity.compact,
    );
  }
}

class _ErrorRetry extends StatelessWidget {
  final String message;
  final VoidCallback onRetry;

  const _ErrorRetry({required this.message, required this.onRetry});

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.error_outline, size: 48, color: Colors.red),
            const SizedBox(height: 12),
            Text(message, textAlign: TextAlign.center),
            const SizedBox(height: 16),
            FilledButton.icon(onPressed: onRetry, icon: const Icon(Icons.refresh), label: const Text('Retry')),
          ],
        ),
      ),
    );
  }
}
