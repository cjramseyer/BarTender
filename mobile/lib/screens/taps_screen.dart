import 'package:flutter/material.dart';

import '../models/keg.dart';
import '../models/tap.dart';
import '../services/api_service.dart';

class TapsScreen extends StatefulWidget {
  final ApiService api;

  const TapsScreen({super.key, required this.api});

  @override
  State<TapsScreen> createState() => _TapsScreenState();
}

class _TapsScreenState extends State<TapsScreen> {
  late Future<_TapsData> _future;
  final Map<int, bool> _pouring = {};

  @override
  void initState() {
    super.initState();
    _future = _load();
  }

  Future<_TapsData> _load() async {
    final results = await Future.wait([
      widget.api.fetchTaps(),
      widget.api.fetchKegs(),
    ]);
    return _TapsData(
      taps: results[0] as List<Tap>,
      kegs: results[1] as List<Keg>,
    );
  }

  void _refresh() {
    setState(() {
      _future = _load();
    });
  }

  Future<void> _pour(Tap tap) async {
    final amount = await showDialog<double>(
      context: context,
      builder: (context) => SimpleDialog(
        title: Text(
            'Pour ${tap.label.isNotEmpty ? tap.label : 'Tap ${tap.number}'}'),
        children: [
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, 8),
            child: const Text('Half Pint (8 oz)'),
          ),
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, 16),
            child: const Text('Pint (16 oz)'),
          ),
          SimpleDialogOption(
            onPressed: () => Navigator.pop(context, 32),
            child: const Text('Large Pour (32 oz)'),
          ),
        ],
      ),
    );
    if (amount == null || !mounted) return;
    setState(() => _pouring[tap.id] = true);
    try {
      await widget.api.pourTap(tapId: tap.id, amount: amount, unit: 'oz');
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Pour recorded')));
      _refresh();
    } catch (error) {
      if (!mounted) return;
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(error.toString())));
    } finally {
      if (mounted) setState(() => _pouring.remove(tap.id));
    }
  }

  @override
  Widget build(BuildContext context) {
    return FutureBuilder<_TapsData>(
      future: _future,
      builder: (context, snapshot) {
        if (snapshot.connectionState == ConnectionState.waiting) {
          return const Center(child: CircularProgressIndicator());
        }
        if (snapshot.hasError) {
          return _ErrorView(
              message: snapshot.error.toString(), onRetry: _refresh);
        }

        final data = snapshot.requireData;
        final kegIndex = {for (final k in data.kegs) k.id: k};

        if (data.taps.isEmpty) {
          return _EmptyView(
              icon: Icons.local_bar,
              message: 'No taps configured',
              onRefresh: _refresh);
        }

        return RefreshIndicator(
          onRefresh: () async => _refresh(),
          child: ListView.separated(
            padding: const EdgeInsets.all(12),
            itemCount: data.taps.length,
            separatorBuilder: (_, __) => const SizedBox(height: 8),
            itemBuilder: (context, index) {
              final tap = data.taps[index];
              final keg = tap.kegId != null ? kegIndex[tap.kegId] : null;
              return Card(
                child: ListTile(
                  leading: CircleAvatar(
                    backgroundColor:
                        Theme.of(context).colorScheme.primaryContainer,
                    child: Text(
                      '${tap.number}',
                      style: TextStyle(
                        fontWeight: FontWeight.bold,
                        color: Theme.of(context).colorScheme.onPrimaryContainer,
                      ),
                    ),
                  ),
                  title: Text(
                      tap.label.isNotEmpty ? tap.label : 'Tap ${tap.number}'),
                  subtitle: keg != null
                      ? Text(
                          '${keg.name}${keg.brewery.isNotEmpty ? " · ${keg.brewery}" : ""}${keg.abv.isNotEmpty ? " · ${keg.abv}% ABV" : ""}')
                      : const Text('No keg assigned',
                          style: TextStyle(fontStyle: FontStyle.italic)),
                  trailing: keg != null
                      ? Wrap(
                          spacing: 6,
                          crossAxisAlignment: WrapCrossAlignment.center,
                          children: [
                            Chip(
                              label: Text(keg.status.replaceAll('_', ' ')),
                              backgroundColor:
                                  keg.statusColor.withValues(alpha: 0.15),
                              side: BorderSide(color: keg.statusColor),
                              labelStyle: TextStyle(
                                  color: keg.statusColor, fontSize: 12),
                            ),
                            IconButton(
                              tooltip: 'Record pour',
                              onPressed: _pouring[tap.id] == true
                                  ? null
                                  : () => _pour(tap),
                              icon: _pouring[tap.id] == true
                                  ? const SizedBox.square(
                                      dimension: 18,
                                      child: CircularProgressIndicator(
                                          strokeWidth: 2))
                                  : const Icon(Icons.local_drink),
                            ),
                          ],
                        )
                      : null,
                ),
              );
            },
          ),
        );
      },
    );
  }
}

class _TapsData {
  final List<Tap> taps;
  final List<Keg> kegs;
  _TapsData({required this.taps, required this.kegs});
}

class _ErrorView extends StatelessWidget {
  final String message;
  final VoidCallback onRetry;

  const _ErrorView({required this.message, required this.onRetry});

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
            FilledButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh),
              label: const Text('Retry'),
            ),
          ],
        ),
      ),
    );
  }
}

class _EmptyView extends StatelessWidget {
  final IconData icon;
  final String message;
  final VoidCallback onRefresh;

  const _EmptyView(
      {required this.icon, required this.message, required this.onRefresh});

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 48, color: Colors.grey),
          const SizedBox(height: 12),
          Text(message, style: const TextStyle(color: Colors.grey)),
          const SizedBox(height: 16),
          OutlinedButton.icon(
            onPressed: onRefresh,
            icon: const Icon(Icons.refresh),
            label: const Text('Refresh'),
          ),
        ],
      ),
    );
  }
}
